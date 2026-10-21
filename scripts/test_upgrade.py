"""Behavioral regressions for changed snapshots, decision lifecycle and queue state."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from cloud_backup import CloudBackup
from migration_archive import build_snapshot
from memory_lifecycle import replacement_patch, validate_lifecycle, restore_records
from readonly_memories import ReadonlyMemories
from test_auto_memory import GPTBrokerRegression


def record(text='current decision', rid=None, **meta):
    return {'id': rid or str(uuid.uuid4()), 'memory': text,
            'metadata': {'scope': 'project', 'project': 'example', 'source_agent': 'manual', **meta}}


def document(records):
    return dict(format='mem0-local-portable-v1', created_at='2026-10-21T00:00:00+00:00',
                user_id='local-owner', embedding_model='fixture', records=records)


class FakeEngine:
    def __init__(self): self.records=[]
    def add(self, text, **kwargs):
        row = record(text, **copy.deepcopy(kwargs['metadata']))
        self.records.append(row)
        return {'results':[{'id':row['id']}]}
    def update(self, rid, metadata):
        next(r for r in self.records if r['id']==rid)['metadata'].update(metadata)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.old,self.new=record('old decision'),record('new decision')
        self.patch=replacement_patch([self.old,self.new],self.old['id'],self.new['id'],'user changed requirement','codex')
        self.old['metadata'].update(self.patch)
    def test_graph_and_cycle(self):
        validate_lifecycle([self.old,self.new])
        self.new['metadata'].update(memory_status='superseded',superseded_by=self.old['id'])
        with self.assertRaisesRegex(ValueError,'cycle'):validate_lifecycle([self.old,self.new])
    def test_scope_boundary(self):
        self.new['metadata']['project']='other'
        with self.assertRaisesRegex(ValueError,'scope'):validate_lifecycle([self.old,self.new])
    def test_restore_remaps_and_repeat_is_idempotent(self):
        engine=FakeEngine()
        result=restore_records(engine,'test',[],[self.old,self.new])
        self.assertTrue(result['complete'])
        self.assertEqual(engine.records[0]['metadata']['memory_status'],'superseded')
        self.assertEqual(engine.records[0]['metadata']['superseded_by'],engine.records[1]['id'])
        repeat=restore_records(engine,'test',engine.records,[self.old,self.new])
        self.assertEqual(repeat['skipped'],2)
        self.assertEqual(len(engine.records),2)
    def test_old_restore_cannot_invalidate_current(self):
        engine=FakeEngine(); local=record(self.old['memory']); engine.records=[local]
        result=restore_records(engine,'test',engine.records,[self.old,self.new])
        self.assertFalse(result['complete'])
        self.assertNotEqual(local['metadata'].get('memory_status'),'superseded')
    def test_restore_crash_keeps_invalidated_fact_hidden(self):
        class Failing(FakeEngine):
            def add(self,text,**kwargs):
                if self.records:raise RuntimeError('synthetic failure')
                return super().add(text,**kwargs)
        engine=Failing(); result=restore_records(engine,'test',[],[self.old,self.new])
        self.assertFalse(result['complete'])
        self.assertEqual(engine.records[0]['metadata']['memory_status'],'superseded')
        self.assertTrue(engine.records[0]['metadata']['supersession_unresolved'])
        good=FakeEngine();good.records=engine.records
        result=restore_records(good,'test',good.records,[self.old,self.new])
        self.assertTrue(result['complete'])
        self.assertFalse(good.records[0]['metadata']['supersession_unresolved'])


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='mem0-cloud-upgrade-');self.root=Path(self.tmp.name)
        self.queue=CloudBackup(self.root); self.queue.root.mkdir(parents=True)
        (self.queue.root/'config.json').write_text(json.dumps(dict(enabled=True,folder_id='folder',retention=30,realtime_enabled=True)))
        self.rows=[record()]
    def tearDown(self):self.tmp.cleanup()
    def emit(self):return self.queue.emit(build_snapshot(document(self.rows)), 'TEST')
    def finish(self):
        plan=self.queue.prepare()
        for item in plan['uploads']:
            target=self.root/(plan['snapshot_id']+'-'+item['role'])
            shutil.copyfile(item['path'],target)
            self.queue.verify_file(plan['snapshot_id'],item['role'],item['role']+'-id',target)
        return self.queue.commit(plan['snapshot_id'])
    def test_same_day_changes_and_single_inflight(self):
        first=self.emit();self.assertTrue(first['emitted'])
        self.assertEqual(self.emit()['reason'],'unchanged')
        self.rows.append(record('second'))
        self.assertEqual(self.emit()['reason'],'in_flight_snapshot')
        self.assertTrue(self.queue.status()['local_changes_pending'])
        self.finish(); self.assertTrue(self.emit()['emitted'])
        plan=self.queue.prepare()
        self.assertIn('previous',[a['role'] for a in plan['uploads']])
        self.assertNotIn('archive',[a['role'] for a in plan['uploads']])
        self.finish();self.assertTrue(self.queue.status()['synced'])
    def test_partial_verification_never_commits(self):
        self.emit();plan=self.queue.prepare()
        with self.assertRaisesRegex(ValueError,'all_files'):self.queue.commit(plan['snapshot_id'])
        item=next(a for a in plan['uploads'] if a['role']=='readable');p=self.root/'bad.md';p.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):self.queue.verify_file(plan['snapshot_id'],'readable','valid-id',p)
        self.assertEqual(self.queue.status()['pending'],1)
    def test_restart_resume_same_transaction(self):
        self.emit();plan=self.queue.prepare();restarted=CloudBackup(self.root)
        self.assertEqual(plan['uploads'],restarted.prepare()['uploads'])
        self.finish();self.assertEqual(restarted.status()['pending'],0)
    def test_readback_must_be_independent(self):
        self.emit();plan=self.queue.prepare();item=plan['uploads'][0]
        with self.assertRaisesRegex(ValueError,'independent'):self.queue.verify_file(plan['snapshot_id'],item['role'],'id',item['path'])
    def test_metadata_timestamp_alone_does_not_emit(self):
        self.emit();self.finish();doc=document(self.rows);doc['created_at']='2026-10-22T00:00:00+00:00'
        self.assertFalse(self.queue.emit(build_snapshot(doc),'TEST')['emitted'])


class QueueTests(GPTBrokerRegression):
    def test_expired_lease_is_retry_pending(self):
        self.auto.scan();job=self.claim()[0]
        with self.auto.db() as db:db.execute("UPDATE chunks SET lease_until=?",(time.time()-1,))
        status=self.auto.status()
        self.assertEqual(status['chunks'].get('leased',0),0)
        self.assertEqual(status['chunks']['retry_pending'],1)
        self.assertEqual(status['needs_processing'],1)
        self.assertEqual(len(self.claim()),1)
    def test_scan_does_not_count_as_gpt_completion(self):
        self.auto.scan();self.assertIsNone(self.auto.status()['last_gpt_completed_at'])
        self.submit(self.claim()[0]);last=self.auto.status()['last_gpt_completed_at']
        self.auto.scan();self.assertEqual(self.auto.status()['last_gpt_completed_at'],last)
        self.assertEqual(self.auto.status()['needs_processing'],0)


class ReadonlyTests(unittest.TestCase):
    def test_preview_hash_and_partition_preserve_history(self):
        with tempfile.TemporaryDirectory(prefix='mem0-readonly-') as temp:
            root=Path(temp);part=ReadonlyMemories(root)
            old,new=record('old'),record('new');old['metadata'].update(replacement_patch([old,new],old['id'],new['id'],'updated','codex'))
            source=root/'source.zip';source.write_bytes(build_snapshot(document([old,new])))
            _,_,preview=part.preview(source,'OTHER')
            class Embedder:
                def embed(self,*args):return [1.0,0.0]
            with self.assertRaisesRegex(ValueError,'preview_changed'):part.install(source,'OTHER','wrong',Embedder())
            part.install(source,'OTHER',preview['sha256'],Embedder(),{'example':'mapped'})
            current=part.search([1,0],{'scope':'project','project':'mapped'},10)
            history=part.search([1,0],{'scope':'project','project':'mapped'},10,True)
            self.assertEqual(len(current),1);self.assertEqual(len(history),2)
            self.assertTrue(current[0]['metadata']['readonly'])
            self.assertFalse((root/'exports').exists())


if __name__=='__main__':unittest.main()
