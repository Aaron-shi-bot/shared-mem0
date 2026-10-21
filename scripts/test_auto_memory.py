"""Synthetic GPT-broker checks. No network, real transcripts or Mem0 DB writes."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from auto_memory import AutoMemory, redact


class FakeStore:
    def __init__(self):
        self.items = {}
        self.writes = 0
        self.crash_after_write = False
        self.lock = threading.Lock()

    def upsert(self, item):
        with self.lock:
            self.writes += 1
            key = item['auto_key']
            memory_id = 'synthetic-' + key
            self.items[key] = {**item, 'id': memory_id}
            if self.crash_after_write:
                self.crash_after_write = False
                raise RuntimeError('synthetic crash after external commit')
            return memory_id

    def delete(self, memory_id, auto_key):
        with self.lock:
            old = self.items.get(auto_key)
            if old and old['id'] == memory_id:
                self.items.pop(auto_key)


class GPTBrokerRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mem0-gpt-fixture-')
        self.root = Path(self.temp.name)
        self.roots = {name: [self.root / 'sources' / name] for name in ('codex','workbuddy','pi','claude')}
        for paths in self.roots.values(): paths[0].mkdir(parents=True)
        self.store = FakeStore()
        self.auto = AutoMemory(self.root / 'data', self.store.upsert, self.store.delete, roots=self.roots)
        # Public installations start with collection disabled. These isolated
        # queue tests explicitly opt in to scanning their synthetic sources.
        self.configure(enabled=True, quiet_seconds=0)
        self.path = self.write_session('codex')

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, **values):
        cfg = self.auto.config(); cfg.update(values)
        self.auto.config_path.write_text(json.dumps(cfg), encoding='utf-8')

    def write_session(self, client, messages=None, filename='session.jsonl', session_id='fixture-session'):
        if messages is None: messages=[('user','已确认虚构图书项目使用 SQLite，仅在本机保存数据。')]
        cwd='D:/Synthetic/Library'
        if client == 'codex': rows=[{'type':'session_meta','payload':{'id':session_id,'cwd':cwd}}]
        elif client == 'pi': rows=[{'type':'session','id':session_id,'cwd':cwd}]
        else: rows=[]
        for index,(role,text) in enumerate(messages):
            base={'timestamp':f'2026-01-01T00:00:{index:02d}Z'}
            if client=='codex': row={**base,'type':'response_item','payload':{'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}}
            elif client=='workbuddy': row={**base,'type':'message','sessionId':session_id,'cwd':cwd,'role':role,'content':text}
            elif client=='pi': row={**base,'type':'message','message':{'role':role,'content':[{'type':'text','text':text}]}}
            else: row={**base,'type':role,'sessionId':session_id,'cwd':cwd,'message':{'role':role,'content':[{'type':'text','text':text}]}}
            rows.append(row)
        path=self.roots[client][0]/filename
        path.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n',encoding='utf-8')
        return path

    def rows(self, table='sessions'):
        with self.auto.db() as db:return [dict(x) for x in db.execute('SELECT * FROM '+table)]

    def claim(self, **kwargs):return self.auto.claim(**kwargs)['jobs']

    def submit(self, job, items=None):
        if items is None: items=[{'kind':'requirement','text':'虚构图书项目已明确采用 SQLite。','evidence':[0]}]
        return self.auto.submit(job['job_id'],job['lease'],job['content_hash'],items)

    def test_four_clients_shared_project_and_persisted_restart(self):
        for client in ('workbuddy','pi','claude'):self.write_session(client)
        self.assertEqual(self.auto.scan()['discovered'],4)
        jobs=self.claim(limit=10)
        self.assertEqual({j['client'] for j in jobs},{'codex','workbuddy','pi','claude'})
        self.assertEqual(len({j['project'] for j in jobs}),1)
        for job in jobs:self.submit(job)
        restarted=AutoMemory(self.root/'data',self.store.upsert,self.store.delete,roots=self.roots)
        self.assertEqual(restarted.status()['chunks'],{'done':4})
        restarted.scan()
        self.assertFalse(restarted.claim(limit=10)['jobs'])
        self.assertEqual(len(self.store.items),4)

    def test_identical_scan_and_submit_are_idempotent(self):
        self.auto.scan(); job=self.claim()[0]
        first=self.submit(job); second=self.submit(job)
        self.assertTrue(second['already_done'])
        self.assertEqual(first['id'],second['id'])
        self.auto.scan();self.assertFalse(self.claim())
        self.assertEqual(self.store.writes,1)

    def test_append_invalidates_lease_and_updates_same_memory(self):
        self.auto.scan();job=self.claim()[0];self.submit(job)
        old_id=next(iter(self.store.items.values()))['id']
        self.write_session('codex',[('user','已确认虚构图书项目使用 SQLite，仅在本机保存数据。'),('user','下一步需要添加借阅提醒功能。')])
        self.auto.scan(); new=self.claim()[0]; self.submit(new,[{'kind':'requirement','text':'下一步添加借阅提醒功能。','evidence':[1]}])
        self.assertEqual(len(self.store.items),1)
        self.assertEqual(next(iter(self.store.items.values()))['id'],old_id)
        with self.assertRaises(ValueError):self.submit(job)

    def test_source_append_outside_unchanged_part_commits_without_completing_session(self):
        self.configure(chunk_chars=2000)
        self.write_session('codex',[('user','甲'*1000),('user','乙'*1000)])
        self.auto.scan();job=self.claim(limit=1)[0]
        self.write_session('codex',[('user','甲'*1000),('user','乙'*1000),('user','新需求：虚构项目需要借阅提醒。')])
        result=self.submit(job)
        self.assertTrue(result['session_needs_rescan'])
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertIsNone(self.rows()[0]['fingerprint'])
        self.assertIsNone(self.rows()[0]['last_success'])
        self.assertEqual(len(self.store.items),1)
        self.auto.scan()
        self.assertEqual(self.rows('chunks')[0]['status'],'done')
        self.assertEqual(self.rows()[0]['status'],'pending')
        for pending in self.claim(limit=10):self.submit(pending)
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_heartbeat_append_before_claim_and_again_before_submit(self):
        self.auto.scan()
        def append_tool():
            with self.path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'response_item','payload':{'type':'function_call','name':'synthetic_status','arguments':'{}'}})+'\n')
        append_tool()
        self.configure(quiet_seconds=3600);self.auto.scan()
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        job=self.claim()[0]
        append_tool()
        result=self.submit(job)
        self.assertTrue(result['session_needs_rescan'])
        self.assertEqual(self.rows('chunks')[0]['status'],'done')
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertIsNone(self.rows()[0]['fingerprint'])
        self.assertFalse(self.claim())
        self.configure(quiet_seconds=0);self.auto.scan()
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertEqual(self.store.writes,1)

    def test_append_that_extends_target_tail_still_rejects_commit(self):
        self.auto.scan();job=self.claim()[0]
        self.write_session('codex',[('user','已确认虚构图书项目使用 SQLite，仅在本机保存数据。'),('user','新决定：新增检索功能。')])
        with self.assertRaisesRegex(ValueError,'片段已变化'):self.submit(job)
        self.assertFalse(self.store.items)
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertEqual(self.rows('chunks')[0]['status'],'pending')
        self.assertIsNone(self.rows('chunks')[0]['lease'])

    def test_append_during_store_write_never_marks_session_complete(self):
        self.auto.scan();job=self.claim()[0]
        original_upsert=self.auto.upsert
        def append_during_upsert(item):
            memory_id=original_upsert(item)
            with self.path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'response_item','payload':{'type':'function_call','name':'synthetic_status','arguments':'{}'}})+'\n')
            return memory_id
        self.auto.upsert=append_during_upsert
        self.assertTrue(self.submit(job)['session_needs_rescan'])
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertIsNone(self.rows()[0]['fingerprint'])
        self.assertIsNone(self.rows()[0]['last_success'])
        self.auto.scan()
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_legacy_descriptor_without_prefix_hash_waits_until_scan(self):
        self.auto.scan()
        desc=json.loads(self.rows()[0]['descriptor']);desc.pop('source_prefix_hash')
        self.auto.record('codex:fixture-session',descriptor=json.dumps(desc))
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'type':'function_call','name':'synthetic_status','arguments':'{}'}})+'\n')
        self.assertFalse(self.claim())
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.auto.scan()
        self.assertIn('source_prefix_hash',json.loads(self.rows()[0]['descriptor']))
        self.submit(self.claim()[0])
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_midfile_change_rejected_even_if_target_fragment_unchanged(self):
        self.configure(chunk_chars=2000)
        self.write_session('codex',[('user','甲'*1000),('user','乙'*1000)])
        self.auto.scan();job=self.claim(limit=1)[0]
        self.write_session('codex',[('user','甲'*1000),('user','丙'*1000),('user','新的追加内容。')])
        with self.assertRaisesRegex(ValueError,'源会话已变化'):self.submit(job)
        self.assertFalse(self.store.items)

    def test_incomplete_appended_tail_cannot_be_submitted(self):
        self.auto.scan();job=self.claim()[0]
        with self.path.open('a',encoding='utf-8') as stream:stream.write('{incomplete')
        with self.assertRaisesRegex(ValueError,'读取不完整'):self.submit(job)
        self.assertFalse(self.store.items)

    def test_crash_after_external_write_retries_without_duplicates(self):
        self.auto.scan();job=self.claim()[0];self.store.crash_after_write=True
        with self.assertRaises(RuntimeError):self.submit(job)
        self.assertNotEqual(self.rows()[0]['status'],'done')
        self.assertEqual(len(self.store.items),1)
        self.submit(job)
        self.assertEqual(len(self.store.items),1)
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_fail_then_new_claim_and_late_fail_cannot_undo_success(self):
        self.auto.scan();job=self.claim()[0]
        self.auto.fail(job['job_id'],job['lease'],'synthetic GPT failure')
        self.assertNotEqual(self.rows()[0]['status'],'done')
        new=self.claim()[0];self.assertNotEqual(job['lease'],new['lease'])
        with self.assertRaises(ValueError):self.submit(job)
        self.submit(new)
        self.auto.fail(new['job_id'],new['lease'],'late network error')
        self.assertEqual(self.rows('chunks')[0]['status'],'done')

    def test_concurrent_claim_exclusive_and_expiry_recoverable(self):
        self.auto.scan()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.claim(limit=1),range(2)))
        self.assertEqual(sum(len(x) for x in results),1)
        job=next(x[0] for x in results if x)
        with self.auto.db() as db:db.execute('UPDATE chunks SET lease_until=?',(time.time()-1,))
        replacement=self.claim()[0];self.assertNotEqual(job['lease'],replacement['lease'])
        with self.assertRaises(ValueError):self.submit(job)
        self.submit(replacement)

    def test_only_all_chunks_done_completes_session(self):
        self.configure(chunk_chars=2000)
        self.write_session('codex',[('user','甲'*1000),('user','乙'*1000),('user','丙'*1000)])
        self.auto.scan();jobs=self.claim(limit=10)
        self.assertGreater(len(jobs),1)
        self.submit(jobs[0]);self.assertEqual(self.rows()[0]['status'],'pending')
        for job in jobs[1:]:self.submit(job)
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_empty_summary_success_is_distinct_from_failure(self):
        self.auto.scan();job=self.claim()[0]
        self.submit(job,[])
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertEqual(self.rows()[0]['memory_count'],0)
        self.assertFalse(self.store.items)

    def test_claim_skips_error_or_never_scanned_waiting_sessions(self):
        self.auto.scan()
        self.auto.record('codex:fixture-session',status='error')
        self.assertFalse(self.claim())
        self.assertEqual(self.rows('chunks')[0]['status'],'pending')
        self.auto.record('codex:fixture-session',status='waiting_idle',descriptor=None)
        self.assertFalse(self.claim())

    def test_claim_source_change_waits_and_stable_scan_recovers(self):
        self.auto.scan()
        self.write_session('codex',[('user','已确认虚构项目添加新的借阅提醒功能。')])
        self.assertFalse(self.claim())
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        chunk=self.rows('chunks')[0]
        self.assertEqual(chunk['status'],'pending')
        self.assertIsNone(chunk['error']);self.assertIsNone(chunk['lease'])
        self.assertFalse(self.auto.status()['attention'])
        self.auto.scan();job=self.claim()[0];self.submit(job)
        self.assertEqual(self.rows()[0]['status'],'done')

    def test_claim_incomplete_read_waits_without_false_error(self):
        self.auto.scan()
        with patch.object(self.auto,'_parse',return_value={'incomplete':True,'warnings':['source_changed']}):
            self.assertFalse(self.claim())
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertEqual(self.rows('chunks')[0]['status'],'pending')
        self.assertFalse(self.auto.status()['attention'])
        self.auto.scan();self.assertEqual(len(self.claim()),1)

    def test_claim_real_failure_remains_visible_and_retryable(self):
        self.auto.scan()
        with patch.object(self.auto,'_part',side_effect=RuntimeError('synthetic parse decoder failed')):
            self.assertFalse(self.claim())
        status=self.auto.status()
        self.assertEqual(status['chunks'],{'error':1})
        self.assertEqual(status['attention'][0]['part'],1)
        self.assertIn('synthetic parse decoder failed',status['attention'][0]['error'])
        self.submit(self.claim()[0])
        self.assertFalse(self.auto.status()['attention'])

    def test_scan_cleans_only_legacy_source_change_errors(self):
        self.configure(chunk_chars=2000)
        self.write_session('codex',[('user','甲'*1000),('user','乙'*1000)])
        self.auto.scan()
        with self.auto.db() as db:
            db.execute("UPDATE chunks SET status='error',error='源会话已变化，请重新扫描后领取' WHERE position=0")
            db.execute("UPDATE chunks SET status='error',error='synthetic permanent decoder failure' WHERE position=1")
        self.auto.record('codex:fixture-session',status='waiting_idle')
        self.configure(quiet_seconds=3600)
        self.auto.scan()
        chunks=self.rows('chunks')
        self.assertEqual(chunks[0]['status'],'pending');self.assertIsNone(chunks[0]['error'])
        self.assertEqual(chunks[1]['status'],'error')
        self.assertEqual(self.rows()[0]['status'],'waiting_idle')
        self.assertEqual(len(self.auto.status()['attention']),1)
        self.assertIn('permanent decoder failure',self.auto.status()['attention'][0]['error'])

    def test_bad_evidence_and_assistant_claims_not_promoted(self):
        self.write_session('codex',[('assistant','助手自称虚构测试已通过，未提供证据。')])
        self.auto.scan();job=self.claim()[0]
        with self.assertRaises(ValueError):self.submit(job,[{'kind':'decision','text':'示例决定。','evidence':[900]}])
        self.submit(job,[{'kind':'decision','text':'助手自称示例测试已通过。','evidence':[0]}])
        self.assertIn('助手报告（未独立复核）',next(iter(self.store.items.values()))['text'])

    def test_secret_redaction_and_reject_output(self):
        fake='SYNTHETIC_CREDENTIAL_VALUE_94273'
        formats=['ANTHROPIC_AUTH_TOKEN='+fake,'SECRET_KEY: '+fake,'token='+fake,'密码是'+fake,'<api_key>'+fake+'</api_key>','{"api_key":"'+fake+'"}','Authorization: Bearer '+fake]
        for text in formats:
            with self.subTest(format=text.split(fake)[0]):self.assertNotIn(fake,redact(text))
        self.write_session('codex',[('user','已确认图书项目使用 SQLite。\n'+'\n'.join(formats))])
        self.auto.scan();job=self.claim()[0];self.assertNotIn(fake,json.dumps(job))
        with self.assertRaises(ValueError):self.submit(job,[{'kind':'requirement','text':'ANTHROPIC_AUTH_TOKEN='+fake,'evidence':[0]}])
        self.assertFalse(self.store.items)
        with self.auto.db() as db:dump='\n'.join(db.iterdump())
        self.assertNotIn(fake,dump)

    def test_connection_and_bare_token_redaction(self):
        samples=['DEPLOY_TOKEN = SYNTHETIC_TOKEN_85491',
                 'DB_PASSWORD: SYNTHETIC_PASSWORD_85491',
                 'eyJSYNTHETICHEADER.signaturepayload.syntheticSignatureValue',
                 'vless://00000000-0000-0000-0000-000000000000@example.invalid:443?type=tcp',
                 'trojan://synthetic-password@example.invalid:443',
                 'gho_SYNTHETICVALUE1234567890']
        for sample in samples:
            with self.subTest(sample=sample.split(':')[0]):
                self.assertNotEqual(redact(sample),sample)
                self.assertNotIn('SYNTHETIC',redact(sample))

    def test_normal_technical_token_discussion_is_not_removed(self):
        for sample in ['项目需要实现令牌刷新，不应保存原始凭据。',
                       '令牌刷新机制使用 `refresh_token`，密码长度限制为 `32`。',
                       '记录 token 使用量和延迟，不保存认证信息。',
                       'Please implement password reset and token rotation.']:
            self.assertEqual(redact(sample),sample)

    def test_markdown_credential_rows_and_context_values_are_redacted(self):
        fake='SYNTHETIC_SECRET_VALUE_92648'
        samples=[f'| 用户名 | `{fake}` |',f'| **密码** | `{fake}` |',
                 f'| Username | `{fake}` |',f'| Password | `{fake}` |',
                 f'| API key | {fake} |',f'你密码里的 `{fake}` 不要发给其他人。',
                 f'The password contains `{fake}` for this fixture.']
        for sample in samples:
            with self.subTest(prefix=sample.split(fake)[0]):
                self.assertNotIn(fake,redact(sample))
        self.write_session('workbuddy',[('user','\n'.join(samples))])
        self.auto.scan();job=self.claim(client='workbuddy')[0]
        self.assertNotIn(fake,json.dumps(job))
        for sample in samples:
            with self.subTest(output=sample.split(fake)[0]):
                with self.assertRaises(ValueError):self.submit(job,[{'kind':'reported_result','text':sample,'evidence':[0]}])
        self.assertFalse(self.store.items)

    def test_langsmith_langfuse_keys_and_markdown_escapes_are_redacted(self):
        fake='SYNTHETIC_ONLY_SECRET_VALUE_76245'
        for prefix in ('lsv2_pt_',r'lsv2\_pt\_','ls__',r'ls\_\_','lf_secret_','sk-lf-','pk-lf-'):
            with self.subTest(prefix=prefix):
                sample='已有连接信息 '+prefix+fake+' 已填写。'
                self.assertNotIn(fake,redact(sample))
        self.write_session('codex',[('user','测试凭据：'+r'lsv2\_pt\_'+fake)])
        self.auto.scan();job=self.claim()[0]
        self.assertNotIn(fake,json.dumps(job))
        with self.assertRaises(ValueError):self.submit(job,[{'kind':'decision','text':'测试值 '+r'lsv2\_pt\_'+fake,'evidence':[0]}])

    def test_project_alias_reassignment_requeues_and_moves(self):
        self.auto.scan();self.submit(self.claim()[0])
        cfg=json.loads(self.auto.projects_path.read_text(encoding='utf-8'))
        cfg['aliases'].append({'path':'D:/Synthetic/Library','id':'shared-library'})
        self.auto.projects_path.write_text(json.dumps(cfg),encoding='utf-8')
        self.auto.scan();job=self.claim()[0]
        self.assertEqual(job['project'],'shared-library');self.submit(job)
        self.assertEqual(next(iter(self.store.items.values()))['project'],'shared-library')
        self.assertEqual(len(self.store.items),1)

    def test_duplicate_session_paths_do_not_double_queue(self):
        self.write_session('codex',filename='copy.jsonl')
        self.assertEqual(self.auto.scan()['discovered'],1)
        self.assertEqual(len(self.claim(limit=10)),1)

    def test_malformed_and_unreadable_sources_do_not_claim_completion(self):
        with self.path.open('a',encoding='utf-8') as stream:stream.write('{malformed\n')
        self.auto.scan();self.assertEqual(self.rows()[0]['status'],'error');self.assertFalse(self.claim())
        unreadable={'client':'pi','session_id':'denied','path':'X:/denied.jsonl','status':'unreadable'}
        with patch('session_sources.discover_sessions',return_value=iter([unreadable,unreadable])):
            result=self.auto.scan()
        self.assertNotIn('error',result)
        self.assertTrue(any(r['session_id']=='denied' and r['status']=='error' for r in self.rows()))

    def test_ledger_contains_no_raw_text_and_can_snapshot_restore(self):
        raw='DO_NOT_PERSIST_RAW_TRANSCRIPT_SENTINEL_84539'
        self.write_session('codex',[('user',raw+'。已确认虚构项目使用 SQLite。')])
        self.auto.scan();job=self.claim()[0];self.assertIn(raw,json.dumps(job))
        self.submit(job)
        snapshot=self.root/'ledger-backup.sqlite'
        with self.auto.db() as source:
            with closing(sqlite3.connect(snapshot)) as dest:source.backup(dest)
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute("SELECT count(*) FROM chunks WHERE status='done'").fetchone()[0],1)
            self.assertNotIn(raw,'\n'.join(db.iterdump()))
        self.assertNotIn(raw,json.dumps(self.store.items))


if __name__=='__main__':unittest.main(verbosity=2)
