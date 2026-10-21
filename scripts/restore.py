"""Preview or merge a portable ZIP; legacy encrypted files remain readable."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from migration_archive import _validate_zip, extract_assets, restore_records, restore_native_and_projects
from settings import API_TOKEN, BASE_URL, DATA_ROOT

p = argparse.ArgumentParser(description='预览或恢复共享记忆。新 ZIP 无需口令，旧 .enc 需原 recovery.key。')
p.add_argument('--backup', required=True)
p.add_argument('--key', help='仅旧加密备份需要')
p.add_argument('--apply', action='store_true')
p.add_argument('--extract-to', help='原生记忆资产提取到一个新的空目录')
p.add_argument('--restore-native', action='store_true', help='合并项目映射和原生记忆，需同时指定 --apply 与 --extract-to')
a = p.parse_args()
if a.restore_native and not (a.apply and a.extract_to):
    p.error('--restore-native requires --apply and --extract-to')
path = Path(a.backup)
if path.stat().st_size > 240 * 1024 * 1024:
    p.error('备份超过大小上限')
raw = path.read_bytes()
if not raw.startswith(b'PK'):
    if not a.key:
        p.error('旧加密格式需要 --key；普通 ZIP 不需要密钥')
    from cryptography.fernet import Fernet
    raw = Fernet(Path(a.key).read_bytes().strip()).decrypt(raw)
bodies, doc, _, _ = _validate_zip(raw)
from memory_lifecycle import validate_lifecycle
validate_lifecycle(doc['records'])
print(json.dumps({'created_at': doc['created_at'], 'records': len(doc['records']),
                  'superseded': sum(r['metadata'].get('memory_status') == 'superseded' for r in doc['records']),
                  'apply': a.apply}, ensure_ascii=False))
if a.extract_to:
    print(json.dumps(extract_assets(raw, Path(a.extract_to)), ensure_ascii=False))
if a.apply:
    report = restore_records(raw, BASE_URL, API_TOKEN)
    if a.restore_native:
        report['native'] = restore_native_and_projects(Path(a.extract_to), DATA_ROOT, Path.home(), {})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get('complete'):
        raise SystemExit(2)
