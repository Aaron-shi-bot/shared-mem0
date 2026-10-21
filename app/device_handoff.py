"""Device discovery metadata, explicit file selection and project handoff context."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from cloud_backup import _atomic_json


class DeviceHandoff:
    def __init__(self, data_root, local_device, readonly):
        self.root = Path(data_root) / 'imports'
        self.local_device = local_device
        self.readonly = readonly

    def catalog(self):
        path = self.root / 'device-catalog.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'checked_at': None, 'devices': []}

    def register(self, devices):
        cleaned = []
        previous = {r['device']: r for r in self.catalog()['devices']}
        for row in devices:
            device = row['device']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', device):
                raise ValueError('invalid_source_device')
            if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', row['file_id']):
                raise ValueError('invalid_drive_file_id')
            if row['name'] != 'CURRENT-' + device + '.zip':
                raise ValueError('not_current_device_snapshot')
            if device == self.local_device:
                continue
            modified = datetime.fromisoformat(row['modified_time'].replace('Z', '+00:00'))
            if modified.tzinfo is None:
                raise ValueError('timezone_required')
            item = {k: row[k] for k in ('device', 'file_id', 'name', 'modified_time', 'size')}
            digest = row.get('sha256')
            old = previous.get(device, {})
            if not digest and old.get('file_id') == item['file_id'] and old.get('modified_time') == item['modified_time']:
                digest = old.get('sha256')
            if digest:
                if not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('invalid_verified_snapshot_hash')
                item['sha256'] = digest
            cleaned.append(item)
        result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'devices': cleaned}
        _atomic_json(self.root / 'device-catalog.json', result)
        return result

    def status(self):
        catalog = self.catalog()
        installed = {r['device']: r for r in self.readonly.sources()}
        available = {r['device']: r for r in catalog['devices']}
        rows = []
        for device in sorted(installed.keys() | available.keys()):
            current, cloud = installed.get(device), available.get(device)
            row = {'device': device, 'installed': current, 'cloud': cloud, 'state': 'not_imported'}
            if current:
                row['state'] = 'installed'
                if cloud and cloud.get('sha256'):
                    row['state'] = 'same_content' if cloud['sha256'] == current.get('sha256') else 'check_for_update'
                elif cloud and current.get('installed_at', current.get('created_at')):
                    # Cloud modified time is only a freshness signal, not a content comparison.
                    remote = datetime.fromisoformat(cloud['modified_time'].replace('Z', '+00:00'))
                    local = datetime.fromisoformat(current.get('installed_at', current.get('created_at')).replace('Z', '+00:00'))
                    if remote.timestamp() > local.timestamp() + 120:
                        row['state'] = 'check_for_update'
            rows.append(row)
        return {'local_device': self.local_device, 'cloud_checked_at': catalog['checked_at'], 'devices': rows,
                'notice': '云端目录时间仅提示可能有更新；下载并预览后才能确认内容差异。导入必须明确选择并确认。'}

    def stage(self, device, filename, content_base64):
        if device == self.local_device or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', device):
            raise ValueError('select_another_device')
        if not filename.lower().endswith('.zip'):
            raise ValueError('zip_required')
        raw = base64.b64decode(content_base64, validate=True)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('snapshot_too_large')
        digest = hashlib.sha256(raw).hexdigest()
        target = self.root / 'inbox' / device / (digest + '.zip')
        target.parent.mkdir(parents=True, exist_ok=True)
        # Hash-addressed staging avoids overwriting another selected file or source.
        if not target.exists():
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(raw)
            temporary.replace(target)
        preview = self.readonly.preview(target, device)[2]
        return {'path': str(target.resolve()), **preview}
