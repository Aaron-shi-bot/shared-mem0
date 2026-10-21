"""Behavioral checks: tail recall, scope isolation, cache atomicity and device versions."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from retrieval import PassageIndex, passages, role
from readonly_memories import ReadonlyMemories
from device_handoff import DeviceHandoff
from migration_archive import build_snapshot

def fixture_tokenizer():
    # Synthetic character tokenizer: deterministic unit tests need no private model cache.
    import string
    vocab = {'[UNK]': 0, **{char: i + 1 for i, char in enumerate(string.printable)}}
    tokenizer = Tokenizer(models.WordLevel(vocab, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Split('', behavior='isolated')
    return tokenizer

def record(text,rid=None,**meta):
    return dict(id=rid or str(uuid.uuid4()),memory=text,metadata=dict(scope='project',project='sample',source_agent='codex',**meta))

def document(rows,date='2026-10-21T01:00:00+00:00'):
    return dict(format='mem0-local-portable-v1',created_at=date,user_id='local-owner',embedding_model='fixture',records=rows)

class FakeDense:
    def __init__(self):
        tokenizer=fixture_tokenizer()
        tokenizer.enable_truncation(128)
        self.model=SimpleNamespace(tokenizer=tokenizer)
        self.calls=0
        self.fail=False
    def embed(self,texts,**kwargs):
        for text in texts:
            self.calls+=1
            if self.fail:raise RuntimeError('simulated_embedding_failure')
            v=np.zeros(64,dtype=np.float32)
            for word in text.split():v[int(hashlib.sha256(word.encode()).hexdigest()[:6],16)%64]+=1
            yield v/(np.linalg.norm(v) or 1)

class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mem0-retrieval-')
        self.root=Path(self.temp.name)
        self.dense=FakeDense()
        self.embedder=SimpleNamespace(dense_model=self.dense,config=SimpleNamespace(model='fixture'))
        self.index=PassageIndex(self.root,self.embedder)
    def tearDown(self):self.temp.cleanup()
    def test_long_tail_and_full_character_coverage(self):
        text=('这是一段需要完整检索的中文 ABC_identifier 超长路径和数字12345。\n'*30)+'最后的特殊约束是ZXFINAL。'
        chunks=passages(text,self.index.tokenizer,128)
        self.assertTrue(all(len(self.index.tokenizer.encode(s).ids)<=128 for s in chunks))
        self.assertIn('ZXFINAL',''.join(chunks))
        # Independent reconstruction, allowing overlap but not dropped text.
        for unit in text.splitlines():
            position=0
            for chunk in chunks:
                if unit[position:position+1] in chunk:
                    while position<len(unit) and unit[position] in chunk:position+=1
            self.assertEqual(position,len(unit))
    def test_long_unpunctuated_multilingual_text_coverage(self):
        text=''.join(str(i)+'中文🙂word/' for i in range(250))
        chunks=passages(text,self.index.tokenizer,128)
        spans=[];last=0
        for chunk in chunks:
            start=text.find(chunk,max(0,last-80));self.assertGreaterEqual(start,0)
            spans.append((start,start+len(chunk)));last=start
        end=0
        for a,b in spans:self.assertLessEqual(a,end);end=max(end,b)
        self.assertEqual(end,len(text))
    def test_tail_recall_scope_and_history(self):
        old=record('TAILMARKER old',memory_status='superseded',superseded_by='current')
        current=record('padding '*300+'TAILMARKER final',rid='current')
        other=record('TAILMARKER secret',rid='other');other['metadata']['project']='other'
        self.index.sync([old,current,other])
        found=self.index.search('TAILMARKER',dict(scope='project',project='sample'))
        self.assertEqual(found[0]['id'],'current');self.assertNotIn(other['id'],[r['id'] for r in found])
        self.assertNotIn(old['id'],[r['id'] for r in found])
        history=self.index.search('TAILMARKER',dict(scope='project',project='sample'),include_history=True)
        self.assertIn(old['id'],[r['id'] for r in history])
    def test_tasks_opt_in_and_global_isolation(self):
        project=record('project PHRASE')
        task=record('task PHRASE');task['metadata'].update(scope='task',task_id='t')
        global_row=record('global PHRASE');global_row['metadata'].update(scope='global',project='global')
        self.index.sync([project,task,global_row])
        found=self.index.search('PHRASE',dict(scope='project',project='sample'),include_tasks=True)
        self.assertEqual({r['id'] for r in found},{project['id'],task['id']})
    def test_metadata_refresh_does_not_reembed(self):
        row=record('stable content');self.index.sync([row]);calls=self.dense.calls
        row['metadata']['source']='new evidence';self.index.sync([row])
        self.assertEqual(self.dense.calls,calls)
        reopened=PassageIndex(self.root,self.embedder);reopened.sync([row]);self.assertEqual(self.dense.calls,calls)
    def test_changed_deleted_and_failed_rebuild_are_atomic(self):
        a,b=record('first'),record('second');self.index.sync([a,b])
        self.dense.fail=True;a['memory']='changed'
        with self.assertRaises(RuntimeError):self.index.sync([a])
        with self.index.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],2)
        self.dense.fail=False;self.index.sync([a])
        with self.index.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],1)
    def test_recency_alone_does_not_confirm_decision(self):
        row=record('new assertion',saved_at='2099-01-01',automatic=True)
        self.assertEqual(role(row,set()),'historical_context')
        self.assertEqual(role(row,{row['id']}),'confirmed_decision')
    def test_device_copies_deduplicate_without_modifying_originals(self):
        local=record('shared PHRASE');remote=copy.deepcopy(local);remote['id']='OTHER:'+remote['id'];remote['metadata'].update(readonly=True,source_device='OTHER')
        original=copy.deepcopy([local,remote]);self.index.sync([local,remote])
        result=self.index.search('PHRASE',dict(scope='project',project='sample'))
        self.assertEqual(len(result),1);self.assertEqual(original,[local,remote])

class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mem0-device-');self.root=Path(self.temp.name)
        self.readonly=ReadonlyMemories(self.root);self.manager=DeviceHandoff(self.root,'LOCAL',self.readonly)
        self.a=record('A');self.b=record('B');self.path=self.root/'source.zip'
    def tearDown(self):self.temp.cleanup()
    def write(self,rows,date='2026-10-21T01:00:00+00:00'):
        self.path.write_bytes(build_snapshot(document(rows,date)))
        return self.readonly.preview(self.path,'OTHER')[2]
    def test_diff_mapping_idempotence_and_source_isolation(self):
        p=self.write([self.a]);self.readonly.install(self.path,'OTHER',p['sha256'],project_map={'sample':'mapped'})
        self.a['memory']='changed';p=self.write([self.a,self.b],'2026-10-21T02:00:00+00:00')
        self.assertEqual(p['changes'],dict(added=1,changed=1,removed=0,unchanged=0))
        self.assertEqual(p['project_map'],{'sample':'mapped'})
        self.readonly.install(self.path,'OTHER',p['sha256'],expected_previous_sha256=p['previous_sha256'])
        self.assertEqual(len(self.readonly.records()),2)
        self.assertTrue(all(r['metadata']['project']=='mapped' for r in self.readonly.records()))
        self.assertFalse((self.root/'exports').exists())
        repeat=self.readonly.preview(self.path,'OTHER')[2];self.assertEqual(repeat['changes']['unchanged'],2)
    def test_reject_old_and_changed_preview(self):
        p=self.write([self.a]);self.readonly.install(self.path,'OTHER',p['sha256'])
        p=self.write([self.a,self.b],'2026-10-22T01:00:00+00:00')
        with self.assertRaisesRegex(ValueError,'installed_source_changed'):self.readonly.install(self.path,'OTHER',p['sha256'],expected_previous_sha256='bad')
        with self.assertRaisesRegex(ValueError,'preview_changed'):self.readonly.install(self.path,'OTHER','bad')
        p=self.write([self.a],'2026-10-20T01:00:00+00:00')
        self.assertTrue(p['older_than_installed'])
        with self.assertRaisesRegex(ValueError,'older_snapshot'):self.readonly.install(self.path,'OTHER',p['sha256'])
    def test_catalog_signals_do_not_install(self):
        self.manager.register([dict(device='OTHER',file_id='1234567890abc',name='CURRENT-OTHER.zip',modified_time='2026-10-21T01:00:00Z',size=100)])
        self.assertEqual(self.manager.status()['devices'][0]['state'],'not_imported')
        self.assertEqual(self.readonly.records(),[])
        with self.assertRaises(ValueError):self.manager.register([dict(device='../bad',file_id='1234567890abc',name='x',modified_time='x',size=100)])

    def test_metadata_mapping_changes_and_global_boundary(self):
        p=self.write([self.a]);self.readonly.install(self.path,'OTHER',p['sha256'])
        self.a['metadata']['source']='new evidence';p=self.write([self.a])
        self.assertEqual(p['changes']['changed'],1)
        self.readonly.install(self.path,'OTHER',p['sha256'])
        self.assertEqual(self.readonly.preview(self.path,'OTHER',{'sample':'mapped'})[2]['changes']['changed'],1)
        with self.assertRaisesRegex(ValueError,'global_project_mapping_forbidden'):
            self.readonly.install(self.path,'OTHER',p['sha256'],project_map={'sample':'global'})

    def test_verified_catalog_hash_avoids_false_update_and_expires(self):
        p=self.write([self.a]);self.readonly.install(self.path,'OTHER',p['sha256'])
        row=dict(device='OTHER',file_id='1234567890abc',name='CURRENT-OTHER.zip',modified_time='2026-10-21T01:13:00Z',size=100,sha256=p['sha256'])
        self.manager.register([row]);self.assertEqual(self.manager.status()['devices'][0]['state'],'same_content')
        del row['sha256'];self.manager.register([row]);self.assertEqual(self.manager.catalog()['devices'][0]['sha256'],p['sha256'])
        row['modified_time']='2099-01-01T01:00:00Z';self.manager.register([row])
        self.assertNotIn('sha256',self.manager.catalog()['devices'][0]);self.assertEqual(self.manager.status()['devices'][0]['state'],'check_for_update')

if __name__=='__main__':unittest.main(verbosity=2)
