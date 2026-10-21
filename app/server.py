"""Local management/agent adapter around the official mem0ai library.

Facts are explicitly curated by agents. Optional infer=True uses the configured
LAN OpenAI-compatible model. One process owns Qdrant and serializes all writes.
"""
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal

from settings import APP_ROOT, CONFIG, DATA_ROOT, API_TOKEN
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from mem0 import Memory
from native_backup import add_memory_assets
from auto_memory import AutoMemory
from backup_schedule import seconds_until_backup, stored_backup_status, run_backup_schedule
from cloud_backup import CloudBackup, error_code as cloud_error_code
from memory_lifecycle import superseded, replacement_patch, restore_records as merge_records
from readonly_memories import ReadonlyMemories
from retrieval import PassageIndex
from device_handoff import DeviceHandoff
from migration_archive import _record_plan, build_snapshot, _snapshot_asset_reason, MigrationError
from native_backup import refresh_native_mirrors

log = logging.getLogger('mem0-local')
lock = threading.RLock()
backup_lock = threading.Lock()
engine = None
ready_error = None
automation = None
backup_status = {'last_local': None, 'last_shared': None, 'error': None}
stop_event = threading.Event()
USER = CONFIG['user_id']
cloud_backup = CloudBackup(DATA_ROOT)
readonly_memories = ReadonlyMemories(DATA_ROOT)
device_handoff = DeviceHandoff(DATA_ROOT, CONFIG['device_id'], readonly_memories)
retrieval_index = None

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(data)
    temp.replace(path)

def get_engine():
    if engine is None:
        raise HTTPException(503, '记忆引擎仍在启动，请查看运行日志。')
    return engine

def make_config():
    qwen = CONFIG['llm']
    active = qwen['enabled']
    return {
        'version': 'v1.1',
        'vector_store': {'provider': 'qdrant', 'config': {
            'collection_name': 'shared_memories',
            'path': str(DATA_ROOT / 'data' / 'qdrant'),
            'embedding_model_dims': 384,
        }},
        'embedder': {'provider': 'fastembed', 'config': {
            'model': CONFIG['embedding_model'], 'embedding_dims': 384,
        }},
        'llm': {'provider': 'openai', 'config': {
            'model': qwen['model'] if active else 'disabled-curated-facts',
            'openai_base_url': qwen['base_url'] if active else 'http://127.0.0.1:9/v1',
            'api_key': qwen.get('api_key') or 'local-no-key',
            'temperature': 0.1, 'max_tokens': 2048,
        }},
        'history_db_path': str(DATA_ROOT / 'data' / 'history.sqlite'),
        'custom_instructions': '保留原文语言。只提取已确认事实、项目决定、约束和交接状态；不要推测，不保存密码或 API 密钥。',
    }

def all_records():
    # Qdrant scroll is paginated: exports must never silently truncate.
    e = get_engine()
    from qdrant_client import models
    offset = None
    records = []
    while True:
        page, offset = e.vector_store.client.scroll(
            collection_name='shared_memories', limit=256, offset=offset,
            scroll_filter=models.Filter(must=[models.FieldCondition(key='user_id', match=models.MatchValue(value=USER))]),
            with_payload=True, with_vectors=False,
        )
        for point in page:
            item = dict(point.payload or {})
            text = item.pop('data', '')
            records.append({'id': str(point.id), 'memory': text, 'metadata': item})
        if offset is None:
            return records

def export_locked():
    records = all_records()
    document = {'format': 'mem0-local-portable-v1', 'created_at': now(),
                'user_id': USER, 'embedding_model': CONFIG['embedding_model'], 'records': records}
    atomic_write(DATA_ROOT / 'exports' / 'memories.json', json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8'))
    grouped = {}
    for r in records:
        md = r['metadata']
        category = md.get('scope', 'project')
        project = md.get('project', 'unclassified')
        folder = 'global' if category == 'global' else f'projects/{project}'
        grouped.setdefault(folder, []).append(r)
    export_root = DATA_ROOT / 'exports' / 'readable'
    expected = set()
    for folder, items in grouped.items():
        path = export_root / folder / 'MEMORY.md'
        expected.add(path)
        lines = ['# 共享记忆（自动导出，请通过管理页或工具修改）', '']
        for r in items:
            md = r['metadata']
            lines += [f"## {r['id']}", f"来源：{md.get('source_agent', '?')} | 范围：{md.get('scope', '?')} | 任务：{md.get('task_id', '')}",
                      f"时间：{md.get('updated_at', md.get('created_at', ''))}", '', r['memory'], '']
        atomic_write(path, '\n'.join(lines).encode('utf-8'))
    if export_root.exists():
        for stale in export_root.rglob('MEMORY.md'):
            if stale not in expected:
                stale.unlink()
    return document

def create_backup(force=True):
    # Serialize the entire operation, including atomic local and NAS writes.
    with backup_lock:
        if not force and seconds_until_backup(
                DATA_ROOT / 'backups' / 'portable' / 'latest.zip',
                CONFIG['backup']['interval_seconds']) > 0:
            return {'skipped': True, **backup_status}
        return _create_backup()

def _create_backup():
    with lock:
        doc = export_locked()
        blob = BytesIO()
        with zipfile.ZipFile(blob, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('memories.json', json.dumps(doc, ensure_ascii=False))
            archive.writestr('README.txt', 'Portable Mem0 records. Preview with python scripts/restore.py --backup <ZIP>. No password is needed. No model API secrets are included.')
            assets = add_memory_assets(archive, DATA_ROOT, APP_ROOT)
            for folder in ['app','scripts']:
                for path in (APP_ROOT/folder).iterdir():
                    if path.is_file() and path.suffix in ('.py','.ps1','.ts','.html'):
                        archive.writestr('deployment/'+folder+'/'+path.name,path.read_bytes())
            archive.writestr('deployment/requirements.lock.txt',(APP_ROOT/'requirements.lock.txt').read_bytes())
            template = json.loads(json.dumps(CONFIG))
            template['llm']['api_key']=''
            archive.writestr('deployment/settings.template.json',json.dumps(template,ensure_ascii=False,indent=2))
        encrypted = blob.getvalue()  # Legacy variable: new backups are ordinary ZIPs.
    filename = datetime.now().strftime('%Y%m%d-%H%M%S') + '.zip'
    local = DATA_ROOT / 'backups' / 'portable' / filename
    atomic_write(local, encrypted)
    atomic_write(local.parent / 'latest.zip', encrypted)
    backup_status['last_local'] = now()
    backup_status['error'] = ('原生记忆备份存在遗漏: '+str(assets['errors'])) if assets['errors'] else None
    backup_status['assets'] = {'files':assets['files'],'sqlite_snapshots':assets['sqlite_snapshots'],'errors':assets['errors']}
    try:
        # Both formats use the exact same memory/asset snapshot. This emits at
        # most once per local day; connector uploads are a separate heartbeat.
        emitted = cloud_backup.emit(blob.getvalue(), CONFIG['device_id'], datetime.fromtimestamp(local.stat().st_mtime).astimezone())
        backup_status['cloud'] = {**cloud_backup.status(), 'emitted': emitted.get('emitted', False)}
    except Exception as exc:
        # Preserve the valid local encrypted backup and surface cloud failure.
        # Do not log exception bodies which might contain rejected secret text.
        code = cloud_error_code(exc)
        try:
            cloud_backup.note_build_failure(exc)
        except Exception:
            pass
        backup_status['cloud'] = {'error': code}
    # Keep the last 48 local snapshots; cloud uses one latest snapshot per device.
    for old in sorted(local.parent.glob('20*.zip'))[:-48]:
        try:
            old.unlink()
        except OSError as exc:
            # Retention cleanup must not prevent copying a valid snapshot to NAS.
            message = f'旧备份清理失败: {type(exc).__name__}: {exc}'
            backup_status['error'] = '; '.join(filter(None, [backup_status['error'], message]))
    shared = CONFIG.get('backup', {}).get('shared_folder', '')
    if shared:
        try:
            target = Path(shared)
            # Do not create a misleading local directory if mapped share is gone.
            if not Path(CONFIG['backup']['share_root']).is_dir():
                raise OSError('NAS 共享目录当前不可访问')
            atomic_write(target / CONFIG['device_id'] / 'latest.zip', encrypted)
            info = {'device_id': CONFIG['device_id'], 'updated_at': now(), 'records': len(doc['records']),
                    'format': doc['format'], 'encrypted': False}
            atomic_write(target / CONFIG['device_id'] / 'status.json', json.dumps(info, ensure_ascii=False, indent=2).encode())
            backup_status['last_shared'] = now()
        except Exception as exc:
            backup_status['error'] = '; '.join(filter(None, [backup_status['error'], f'{type(exc).__name__}: {exc}']))
    return {'records': len(doc['records']), **backup_status}

def prepare_cloud_snapshot():
    """Capture saved memory changes without generating a daily backup or doing network IO."""
    if not cloud_backup._config().get('enabled'):
        return {'enabled': False, 'emitted': False}
    with backup_lock:
        with lock:
            doc = export_locked()
            mirrors = refresh_native_mirrors(DATA_ROOT)
            if mirrors['errors']:
                raise ValueError('native_mirror_incomplete')
            assets = {}
            paths = list((DATA_ROOT / 'native').rglob('*'))
            paths.append(DATA_ROOT / 'automation' / 'projects.json')
            for path in paths:
                if not path.is_file():
                    continue
                name = path.relative_to(DATA_ROOT).as_posix()
                if _snapshot_asset_reason(name) is None:
                    if not path.resolve().is_relative_to(DATA_ROOT.resolve()):
                        raise ValueError('native_asset_outside_data_root')
                    assets[name] = path.read_bytes()
            raw = build_snapshot(doc, assets)
        return cloud_backup.emit(raw, CONFIG['device_id'])


def retry_cloud_snapshots():
    # This worker prepares local snapshots only. The signed-in Codex task uploads.
    while not stop_event.is_set():
        try:
            result = prepare_cloud_snapshot()
            if result.get('emitted'):
                backup_status['cloud'] = cloud_backup.status()
        except Exception as exc:
            code = cloud_error_code(exc)
            backup_status['cloud'] = {'error': code}
            try:
                cloud_backup.note_build_failure(exc)
            except Exception:
                pass
            log.warning('Cloud snapshot retry failed: %s', code)
        stop_event.wait(60)

def initialize():
    global engine, ready_error, automation, retrieval_index
    latest = DATA_ROOT / 'backups' / 'portable' / 'latest.zip'
    shared = CONFIG.get('backup', {}).get('shared_folder', '')
    shared_latest = Path(shared) / CONFIG['device_id'] / 'latest.zip' if shared else None
    backup_status.update(stored_backup_status(latest, shared_latest))
    try:
        # Only the local model cache is consulted after installation.
        os_env = __import__('os').environ
        os_env['HF_HUB_OFFLINE'] = '1'
        engine = Memory.from_config(make_config())
        retrieval_index = PassageIndex(DATA_ROOT, engine.embedding_model)
        with lock:
            export_locked()
            retrieval_index.sync(all_records() + readonly_memories.records())
        automation = AutoMemory(DATA_ROOT, upsert_auto_memory, delete_auto_memory)
        threading.Thread(target=automation.loop,daemon=True).start()
        threading.Thread(target=retry_cloud_snapshots, daemon=True).start()
        def backup_failed(exc):
            backup_status['error'] = f'{type(exc).__name__}: {exc}'
            log.exception('Backup failed')
        if CONFIG['backup'].get('enabled', True):
            run_backup_schedule(latest, CONFIG['backup']['interval_seconds'],
                                create_backup, stop_event, backup_failed)
    except Exception as exc:
        ready_error = f'{type(exc).__name__}: {exc}'
        log.exception('Memory initialization failed')

@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=initialize, daemon=True).start()
    yield
    stop_event.set()
    if automation: automation.stop.set()

app = FastAPI(title='Mem0 本地共享记忆', lifespan=lifespan)

@app.middleware('http')
async def protect(request: Request, call_next):
    if request.headers.get('host', '').split(':')[0] not in ('127.0.0.1', 'localhost'):
        return __import__('starlette.responses', fromlist=['JSONResponse']).JSONResponse({'error':'Invalid host'}, status_code=403)
    if request.url.path.startswith('/api/'):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied, API_TOKEN):
            return __import__('starlette.responses', fromlist=['JSONResponse']).JSONResponse({'error':'Unauthorized'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

class Save(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    source_agent: Literal['codex','workbuddy','pi','claude','manual','import'] = 'manual'
    scope: Literal['global','project','task'] = 'project'
    project: str = ''
    task_id: str = ''
    infer: bool = False
    source: str = ''

def scope_filters(scope, project, task_id=''):
    if scope != 'global' and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}', project or ''):
        raise HTTPException(400, '项目必须使用稳定 ID：英文、数字、点、横线或下划线，最长 80 字符。')
    if scope == 'task' and not task_id:
        raise HTTPException(400, '任务记忆必须指定 task_id。')
    f = {'user_id': USER, 'scope': scope, 'project': 'global' if scope == 'global' else project}
    if scope == 'task': f['task_id'] = task_id
    return f

@app.get('/health')
def health():
    return {'ready': engine is not None, 'error': ready_error, 'service': 'mem0-local', 'version': '2026.10.21.2'}

@app.get('/api/status')
def status():
    try:
        cloud = cloud_backup.status()
    except Exception as exc:
        cloud = {'error': type(exc).__name__}
    return {**health(), 'data_root': str(DATA_ROOT), 'llm_enabled': CONFIG['llm']['enabled'],
            'embedding_model': CONFIG['embedding_model'], 'backup': {**backup_status, 'cloud': {**backup_status.get('cloud', {}), **cloud}},
            'automation': automation.status() if automation else {'ready':False},
            'readonly_sources': readonly_memories.sources(),
            'retrieval': retrieval_index.status() if retrieval_index else {'ready':False},
            'devices': device_handoff.status()}

@app.get('/api/memories')
def list_memories(include_history: bool = True):
    with lock:
        return {'results': [r for r in all_records() if include_history or not superseded(r)]}

@app.get('/api/projects')
def projects():
    with lock:
        counts = {}
        for r in all_records() + readonly_memories.records():
            p = r['metadata'].get('project', 'unclassified')
            counts[p] = counts.get(p, 0) + 1
        registry_path=DATA_ROOT/'automation'/'projects.json'
        registry=json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {'projects':{}}
        labels={p:v.get('label',p) for p,v in registry.get('projects',{}).items()}
        return {'projects': counts,'labels':labels}

@app.post('/api/memories')
def save_memory(data: Save):
    f = scope_filters(data.scope, data.project, data.task_id)
    if data.infer and not CONFIG['llm']['enabled']:
        raise HTTPException(400, 'Qwen 接口尚未配置；请先保存提炼后的事实（infer=false）。')
    # Block obvious credentials, not ordinary project identifiers.
    if re.search(r'-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|(?:api[_-]?key|password|密码)\s*[:=]\s*\S{8,}', data.text, re.I):
        raise HTTPException(400, '内容疑似包含密钥或密码，请移除后再保存。')
    with lock:
        e = get_engine()
        fingerprint = hashlib.sha256((json.dumps(f, sort_keys=True)+data.text.strip()).encode()).hexdigest()
        for r in all_records():
            if r['metadata'].get('content_hash') == fingerprint:
                return {'results': [{'id': r['id'], 'event': 'EXISTS', 'memory': r['memory'],
                                     'metadata': r['metadata']}], 'superseded': superseded(r)}
        metadata = {**f, 'source_agent': data.source_agent, 'task_id': data.task_id,
                    'source': data.source, 'content_hash': fingerprint, 'saved_at': now()}
        metadata.pop('user_id')
        # Using a shared user_id and metadata source_agent allows cross-agent recall.
        # Mem0 inference filters by entity IDs, not arbitrary metadata. A stable
        # run_id prevents future infer=True merges from crossing project/task scopes.
        namespace = hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest()
        result = e.add(data.text.strip(), user_id=USER, run_id=namespace, metadata=metadata, infer=data.infer)
        export_locked()
        return result

class Search(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    scope: Literal['global','project','task'] = 'project'
    project: str = ''
    task_id: str = ''
    limit: int = Field(default=8, ge=1, le=50)
    include_history: bool = False
    include_imported: bool = True
    include_tasks: bool = False

@app.post('/api/search')
def search(data: Search):
    f = scope_filters(data.scope, data.project, data.task_id)
    with lock:
        if retrieval_index is None:
            raise HTTPException(503, '全文索引正在准备，请稍后重试')
        records = all_records() + readonly_memories.records()
        retrieval_index.sync(records)
        return {'results': retrieval_index.search(data.query, f, data.limit, data.include_history, data.include_tasks, data.include_imported),
                'retrieval': retrieval_index.status()}


@app.get('/api/memories/{memory_id}')
def get_memory(memory_id: str):
    with lock:
        for row in all_records() + readonly_memories.records():
            if row['id'] == memory_id:
                return row
    raise HTTPException(404, '找不到记忆')


class HandoffRequest(BaseModel):
    project: str
    query: str = ''


@app.post('/api/handoff')
def handoff(data: HandoffRequest):
    scope_filters('project', data.project)
    questions = {'当前要求': '当前已确认的需求、约束和决定是什么？',
                 '已有取舍': '哪些方案已经否定、取消、替代或明确不做？',
                 '进展和验证': '项目做到哪一步，哪些已验证，哪些尚未验证？',
                 '下一步': '下一步待办、阻塞和待确认的问题是什么？'}
    if data.query.strip():
        questions = {'相关交接': data.query}
    sections = {}
    for label, question in questions.items():
        found = search(Search(project=data.project, query=question, include_tasks=True, limit=4))['results']
        sections[label] = [{'id': r['id'], 'excerpt': r['retrieval']['matched_passage'],
                            'metadata': r['metadata'], 'retrieval': r['retrieval']} for r in found]
    preferences = search(Search(scope='global', query='用户的工作方式、沟通偏好、文件组织要求', limit=3))['results']
    auto = automation.status() if automation else {}
    return {'project': data.project, 'sections': sections,
            'preferences': preferences, 'devices': device_handoff.status(),
            'pending_sessions': auto.get('needs_processing'), 'last_gpt_completed_at': auto.get('last_gpt_completed_at'),
            'notice': '按来源和已确认替代关系核实。摘要不等于已实现；另一设备未导入或本机尚待整理的内容可能不在结果中。可按 id 读取完整原文。'}


class Supersede(BaseModel):
    memory_id: str
    replacement_id: str
    reason: str = Field(min_length=1, max_length=2000)
    source_agent: str = 'manual'


@app.post('/api/memories/supersede')
def supersede_memory(data: Supersede):
    with lock:
        try:
            patch = replacement_patch(all_records(), data.memory_id, data.replacement_id, data.reason, data.source_agent)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if patch:
            get_engine().update(data.memory_id, metadata=patch)
            export_locked()
        return {'superseded': True, 'id': data.memory_id, 'replacement_id': data.replacement_id}


class RestoreDocument(BaseModel):
    document: dict


@app.post('/api/restore/records')
def restore_document(data: RestoreDocument):
    try:
        _record_plan(data.document)
        with lock:
            result = merge_records(get_engine(), USER, all_records(), data.document['records'])
            export_locked()
            return result
    except (ValueError, MigrationError) as exc:
        raise HTTPException(409, str(exc))


class ImportSnapshot(BaseModel):
    path: str
    device: str
    expected_sha256: str = ''
    project_map: dict[str, str] | None = None
    expected_previous_sha256: str | None = None


@app.post('/api/imports/preview')
def preview_import(data: ImportSnapshot):
    if data.device == CONFIG['device_id']:
        raise HTTPException(409, '请选择另一台设备；本机恢复使用恢复工具')
    try:
        return readonly_memories.preview(data.path, data.device, data.project_map)[2]
    except Exception as exc:
        raise HTTPException(400, cloud_error_code(exc))


@app.post('/api/imports/apply')
def apply_import(data: ImportSnapshot):
    preview_import(data)
    try:
        with lock:
            return readonly_memories.install(data.path, data.device, data.expected_sha256,
                                              None, data.project_map, data.expected_previous_sha256)
    except Exception as exc:
        raise HTTPException(409, cloud_error_code(exc))


class DeviceCatalog(BaseModel):
    devices: list[dict] = Field(max_length=100)


@app.get('/api/devices')
def devices():
    return device_handoff.status()


@app.post('/api/devices/catalog')
def register_devices(data: DeviceCatalog):
    try:
        return device_handoff.register(data.devices)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, cloud_error_code(exc))


class StageSnapshot(BaseModel):
    device: str
    filename: str
    content_base64: str = Field(max_length=45 * 1024 * 1024)


@app.post('/api/imports/stage')
def stage_import(data: StageSnapshot):
    try:
        return device_handoff.stage(data.device, data.filename, data.content_base64)
    except Exception as exc:
        raise HTTPException(400, cloud_error_code(exc))


@app.post('/api/cloud/prepare')
def cloud_prepare():
    return prepare_cloud_snapshot()

@app.delete('/api/memories/{memory_id}')
def delete(memory_id: str):
    with lock:
        e = get_engine()
        if not any(r['id'] == memory_id for r in all_records()):
            raise HTTPException(404, '找不到记忆')
        if any(r['metadata'].get('superseded_by') == memory_id for r in all_records()):
            raise HTTPException(409, '该记忆被历史替代关系引用，请保留以便追溯。')
        result = e.delete(memory_id)
        export_locked()
        return result

@app.post('/api/backup')
def backup():
    return create_backup()

def upsert_auto_memory(data):
    f = scope_filters('project', data['project'])
    with lock:
        e = get_engine()
        old = next((r for r in all_records() if r['metadata'].get('auto_key') == data['auto_key']), None)
        if old and superseded(old):
            if data.get('reviewed_superseded') or old['memory'] == data['text']:
                return old['id']
            raise ValueError('superseded_memory_requires_review:' + str(old['metadata'].get('superseded_by', '')))
        metadata = {**f, 'source_agent': data['source_agent'], 'source': data['source'],
                    'task_id': data['task_id'], 'auto_key': data['auto_key'], 'automatic': True,
                    'summarizer': 'GPT task', 'session_id': data['session_id'], 'part': data['part'],
                    'saved_at': now(), 'content_hash': hashlib.sha256(data['text'].encode()).hexdigest()}
        metadata.pop('user_id')
        if old and old['metadata'].get('project') == data['project']:
            if old['memory'] != data['text']:
                e.update(old['id'], text=data['text'], metadata=metadata)
            memory_id = old['id']
        else:
            result = e.add(data['text'], user_id=USER,
                run_id=hashlib.sha256(json.dumps(f,sort_keys=True).encode()).hexdigest(), metadata=metadata, infer=False)
            memory_id = result['results'][0]['id']
            if old: e.delete(old['id'])
        export_locked()
        return memory_id

def delete_auto_memory(memory_id, auto_key):
    with lock:
        owned = next((r for r in all_records() if r['id']==memory_id and r['metadata'].get('auto_key')==auto_key), None)
        if owned:
            if superseded(owned) or any(r['metadata'].get('superseded_by') == memory_id for r in all_records()):
                return
            get_engine().delete(memory_id)
            export_locked()

def get_automation():
    if automation is None: raise HTTPException(503,'自动采集器仍在启动')
    return automation

@app.get('/api/automation/status')
def automation_status():
    return get_automation().status()

class ScanRequest(BaseModel):
    force: bool = False
    session_key: str | None = Field(default=None, max_length=360,
        pattern=r'^(?:codex|workbuddy|pi|claude):[^\r\n]{1,350}$')

@app.post('/api/automation/scan')
def automation_scan(data: ScanRequest | None = None):
    worker=get_automation()
    options=(data or ScanRequest()).model_dump()
    threading.Thread(target=worker.scan,kwargs=options,daemon=True).start()
    return {'queued':True,**options}

class Claim(BaseModel):
    worker: str='gpt'
    limit: int=2
    max_chars: int=32000
    client: str | None=None

@app.post('/api/automation/claim')
def automation_claim(data:Claim):
    return get_automation().claim(**data.model_dump())

class Submission(BaseModel):
    job_id: str
    lease: str
    content_hash: str
    items: list[dict]
    reviewed_superseded: bool = False

@app.post('/api/automation/submit')
def automation_submit(data:Submission):
    try: return get_automation().submit(**data.model_dump())
    except ValueError as exc: raise HTTPException(409,str(exc))

class FailedJob(BaseModel):
    job_id: str
    lease: str
    error: str

class AutoRestore(BaseModel):
    auto_key: str = Field(pattern=r'^[a-f0-9]{64}$')
    text: str = Field(min_length=1,max_length=16000)
    source_agent: Literal['codex','workbuddy','pi','claude']
    project: str
    task_id: str
    session_id: str
    part: int = Field(ge=1)
    source: str

@app.post('/api/automation/restore-record')
def restore_auto_record(data:AutoRestore):
    try:
        return {'id':upsert_auto_memory(data.model_dump())}
    except ValueError as exc:
        raise HTTPException(409, str(exc))

@app.post('/api/automation/fail')
def automation_fail(data:FailedJob):
    get_automation().fail(**data.model_dump())
    return {'retryable':True}

@app.get('/', response_class=HTMLResponse)
def dashboard():
    return (APP_ROOT/'app'/'dashboard.html').read_text(encoding='utf-8').replace('__API_TOKEN__', API_TOKEN)

@app.get('/recovery', response_class=HTMLResponse)
def recovery_document():
    return (APP_ROOT/'app'/'恢复说明.html').read_text(encoding='utf-8')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=CONFIG['port'], log_level='info')
