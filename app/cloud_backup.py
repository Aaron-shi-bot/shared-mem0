"""Daily immutable memory snapshots; cloud IO belongs to the signed-in connector.

The service emits once per local day. The GPT heartbeat only prepares, uploads,
downloads for verification, and acknowledges pending snapshots. No credentials,
signed download URLs, recovery keys, or raw conversations are stored here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
import zipfile

from migration_archive import create_snapshot, open_snapshot
from migration_archive import _validate_zip, _snapshot_asset_reason

FORMAT = 'mem0-google-drive-outbox-v1'
ID_RE = re.compile(r'[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}')
DRIVE_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,256}')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def error_code(exc):
    code = getattr(exc, 'code', None) or str(exc)
    return code if re.fullmatch(r'[a-z0-9_-]{1,100}', code) else type(exc).__name__


def _reparse(path):
    if path.is_symlink():
        return True
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == 'nt' else False


def _line(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ')


def _readable(document, snapshot_id, warning_count):
    records = document['records']
    lines = ['# 共享记忆快照', '', f'快照：{snapshot_id}',
             f"记录时间：{_line(document.get('created_at'))}", f'记忆条数：{len(records)}',
             '内容是已保存的项目记忆；历史助手结果仍以其原有来源标记为准。', '']
    if warning_count:
        lines += [f'注意：本次原生记忆资产有 {warning_count} 项收集警告，请检查本机备份状态。', '']
    grouped = {}
    for record in records:
        meta = record.get('metadata', {})
        project = '全局偏好' if meta.get('scope') == 'global' else meta.get('project', 'unclassified')
        grouped.setdefault(str(project), []).append(record)
    for project, items in sorted(grouped.items()):
        lines += [f'## {_line(project)}', '']
        for record in items:
            meta = record.get('metadata', {})
            lines += [f"### 记录 {_line(record['id'])}",
                      f"来源：{_line(meta.get('source_agent', '?'))} | 范围：{_line(meta.get('scope', '?'))} | 任务：{_line(meta.get('task_id'))}",
                      f"出处：{_line(meta.get('source'))}",
                      f"状态：{_line(meta.get('memory_status', 'active'))} | 替代记录：{_line(meta.get('superseded_by'))}",
                      '', record['memory'], '']
    return ('\n'.join(lines) + '\n').encode('utf-8')


class CloudBackup:
    def __init__(self, data_root):
        self.data_root = Path(data_root).resolve()
        self.root = self.data_root / 'backups' / 'google-drive'
        self.snapshots = self.root / 'snapshots'

    def _check_store(self):
        for path in (self.root.parent, self.root, self.snapshots):
            if path.exists() and (_reparse(path) or not path.resolve().is_relative_to(self.data_root)):
                raise ValueError('unsafe_cloud_storage_path')

    def _config(self):
        self._check_store()
        value = _json(self.root / 'config.json', {})
        if not isinstance(value, dict):
            raise ValueError('cloud_config_invalid')
        if not value.get('enabled', False):
            return {'enabled': False}
        if not DRIVE_ID_RE.fullmatch(str(value.get('folder_id', ''))):
            raise ValueError('cloud_folder_id_invalid')
        retention = value.get('retention', 30)
        if type(retention) is not int or retention < 1 or retention > 365:
            raise ValueError('cloud_retention_invalid')
        return {'enabled': True, 'folder_id': value['folder_id'], 'retention': retention,
                'realtime_enabled': bool(value.get('realtime_enabled', False)),
                'upload_mode': 'codex', 'background_upload_enabled': False}

    @contextmanager
    def _locked(self):
        self._check_store()
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.queue.lock').open('a+b') as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _directory(self, snapshot_id):
        self._check_store()
        if not isinstance(snapshot_id, str) or not ID_RE.fullmatch(snapshot_id):
            raise ValueError('invalid_snapshot_id')
        path = self.snapshots / snapshot_id
        if (self.snapshots.exists() and _reparse(self.snapshots)) or (path.exists() and _reparse(path)) or not path.resolve().is_relative_to(self.snapshots.resolve()):
            raise ValueError('unsafe_snapshot_path')
        return path

    def _remove_directory(self, path):
        # Recursive cleanup is restricted to a checked direct child. Refuse all
        # reparse points, including descendants, before deleting anything.
        path = Path(path)
        self._check_store()
        if _reparse(self.snapshots) or _reparse(path) or path.resolve().parent != self.snapshots.resolve():
            raise ValueError('unsafe_retention_path')
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if _reparse(Path(directory) / name):
                    raise ValueError('unsafe_retention_reparse')
        shutil.rmtree(path)

    def _entries(self):
        if not self.snapshots.is_dir():
            return []
        results = []
        for path in sorted(self.snapshots.iterdir()):
            if path.name.startswith('.building-'):
                continue  # An interrupted build is never an uploadable snapshot.
            directory = self._directory(path.name)
            manifest = _json(directory / 'manifest.json')
            if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('snapshot_id') != path.name:
                raise ValueError('invalid_snapshot_manifest')
            for filename in ('snapshot.zip', 'MEMORY.md'):
                asset = directory / filename
                if not asset.is_file() or _reparse(asset) or asset.resolve().parent != directory.resolve():
                    raise ValueError('missing_snapshot_asset')
            receipt = _json(directory / 'receipt.json')
            if receipt is not None and (not isinstance(receipt, dict) or receipt.get('snapshot_id') != path.name
                                        or receipt.get('sha256') != manifest.get('sha256')):
                raise ValueError('invalid_snapshot_receipt')
            results.append((directory, manifest, receipt))
        return results

    def emit(self, raw_backup_zip, device_id, instant=None):
        """Called by the existing daily backup pass, using its same source ZIP."""
        config = self._config()
        if not config['enabled']:
            return {'enabled': False, 'emitted': False}
        instant = instant or datetime.now().astimezone()
        day = instant.astimezone().date().isoformat()
        device = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(device_id))[:80] or 'device'
        bodies, document, _, _ = _validate_zip(raw_backup_zip)
        stable = {'records': sorted(document['records'], key=lambda r: r['id']),
                  'assets': {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()
                             if _snapshot_asset_reason(name) is None}}
        signature = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._locked():
            entries = self._entries()
            prior_source = _json(self.root / 'source.json', {})
            _atomic_json(self.root / 'source.json', {'content_signature': signature, 'records': len(document['records']),
                         'checked_at': _now(), 'changed_at': prior_source.get('changed_at') if prior_source.get('content_signature') == signature else _now()})
            if config.get('realtime_enabled'):
                if entries and entries[-1][1].get('content_signature') == signature:
                    return {'enabled': True, 'emitted': False, 'reason': 'unchanged'}
                if any(receipt is None for _, _, receipt in entries):
                    return {'enabled': True, 'emitted': False, 'reason': 'in_flight_snapshot', 'local_changes_pending': True}
            else:
                existing = next((m for _, m, _ in entries if m['local_date'] == day), None)
                if existing:
                    return {'enabled': True, 'emitted': False, 'snapshot_id': existing['snapshot_id'], 'reason': 'already_emitted_today'}
            self.snapshots.mkdir(parents=True, exist_ok=True)
            snapshot_id = instant.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:12]
            temporary = self.snapshots / ('.building-' + snapshot_id)
            temporary.mkdir()
            try:
                created = create_snapshot(raw_backup_zip, temporary / 'snapshot.zip')
                checked, manifest = open_snapshot(temporary / 'snapshot.zip')
                with zipfile.ZipFile(io.BytesIO(checked)) as archive:
                    document = json.loads(archive.read('memories.json'))
                readable = _readable(document, snapshot_id, manifest.get('asset_warning_count', 0))
                (temporary / 'MEMORY.md').write_bytes(readable)
                metadata = {'format': FORMAT, 'snapshot_id': snapshot_id,
                            'created_at': _now(), 'backup_created_at': manifest['backup_created_at'],
                            'local_date': day, 'device_id': device, 'records': manifest['records'],
                            'content_signature': signature,
                            'asset_warning_count': manifest.get('asset_warning_count', 0),
                            'sha256': created['sha256'], 'size': created['size'],
                            'readable_sha256': hashlib.sha256(readable).hexdigest(), 'readable_size': len(readable),
                            'zip_name': f'mem0-{device}-{snapshot_id}.zip', 'readable_name': f'MEMORY-{device}.md'}
                _atomic_json(temporary / 'manifest.json', metadata)
                os.replace(temporary, self._directory(snapshot_id))
            finally:
                if temporary.exists():
                    self._remove_directory(temporary)
            (self.root / 'build-error.json').unlink(missing_ok=True)
            self._prune(config['retention'])
            return {'enabled': True, 'emitted': True, **metadata}

    def status(self):
        config = self._config()
        entries = self._entries()
        state = self._view_state(entries)
        source = _json(self.root / 'source.json', {})
        pending = sum(receipt is None for _, _, receipt in entries)
        newer = bool(source and (not entries or source.get('content_signature') != entries[-1][1].get('content_signature')))
        receipts = [(m, r) for _, m, r in entries if r]
        latest = max(receipts, key=lambda pair: pair[1]['uploaded_at'])[0] if receipts else {}
        return {'enabled': config['enabled'], 'folder_id': config.get('folder_id'),
                'upload_mode': 'codex', 'background_upload_enabled': False,
                'realtime_enabled': config.get('realtime_enabled', False),
                'local_changes_pending': newer, 'synced': not pending and not newer and bool(receipts),
                'source_records': source.get('records'), 'source_checked_at': source.get('checked_at'),
                'verified_records': latest.get('records'), 'verified_content_at': latest.get('backup_created_at'),
                'snapshots': len(entries), 'pending': sum(receipt is None for _, _, receipt in entries),
                'last_success': state.get('last_success'), 'last_error': state.get('last_error'),
                'build_error': _json(self.root / 'build-error.json'),
                'latest_snapshot_id': entries[-1][1]['snapshot_id'] if entries else None,
                'readable_file_id': state.get('readable_file_id'),
                'current_file_id': state.get('current_file_id'), 'previous_file_id': state.get('previous_file_id')}

    def note_build_failure(self, exc):
        with self._locked():
            _atomic_json(self.root / 'build-error.json', {'code': error_code(exc), 'time': _now()})

    def retry_latest(self, backup_path, key_path, device_id):
        """Retry an existing local encrypted backup, never create a new backup.

        The backup's timestamp defines its original day, so an hourly consumer
        cannot manufacture new daily snapshots from an unchanged old export.
        Caller serializes this with the service's backup writer.
        """
        if not self._config()['enabled']:
            return {'enabled': False, 'emitted': False}
        backup_path = Path(backup_path)
        if not backup_path.is_file():
            return {'enabled': True, 'emitted': False, 'reason': 'no_local_backup'}
        info = backup_path.stat()
        instant = datetime.fromtimestamp(info.st_mtime).astimezone()
        day = instant.date().isoformat()
        if any(manifest['local_date'] == day for _, manifest, _ in self._entries()):
            return {'enabled': True, 'emitted': False, 'reason': 'already_emitted_for_backup'}
        if info.st_size > 240 * 1024 * 1024:
            raise ValueError('local_backup_size_limit')
        encrypted = backup_path.read_bytes()
        after = backup_path.stat()
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('local_backup_changed_retry')
        from cryptography.fernet import Fernet
        plain = Fernet(Path(key_path).read_bytes().strip()).decrypt(encrypted)
        return self.emit(plain, device_id, instant=instant)

    def prepare(self):
        """Read only; never creates a backup or changes its daily schedule."""
        config = self._config()
        if not config['enabled']:
            return {'enabled': False, 'pending': False}
        if config.get('realtime_enabled'):
            with self._locked():
                return self._prepare_rolling(config)
        entries = self._entries()
        state = self._view_state(entries)
        for directory, manifest, receipt in entries:
            if receipt is not None:
                continue
            if _digest(directory / 'snapshot.zip') != manifest['sha256'] or _digest(directory / 'MEMORY.md') != manifest['readable_sha256']:
                raise ValueError('snapshot_integrity_failed')
            return {'enabled': True, 'pending': True, **manifest,
                    'folder_id': config['folder_id'], 'zip_path': str(directory / 'snapshot.zip'),
                    'readable_path': str(directory / 'MEMORY.md'), 'readable_file_id': state.get('readable_file_id')}
        return {'enabled': True, 'pending': False}

    def _prepare_rolling(self, config):
        entries = self._entries()
        state = self._view_state(entries)
        for directory, manifest, receipt in entries:
            if receipt:
                continue
            transaction = _json(directory / 'transaction.json')
            if transaction is None:
                verified = [(p, m, r) for p, m, r in entries if r]
                previous = max(verified, key=lambda x: x[2]['uploaded_at']) if verified else None
                device = manifest['device_id']
                uploads = []
                def asset(role, name, path, sha, size, file_id=None):
                    uploads.append(dict(role=role, name=name, path=str(path), sha256=sha, size=size, file_id=file_id))
                if previous:
                    p, m, _ = previous
                    asset('previous', f'PREVIOUS-{device}.zip', p / 'snapshot.zip', m['sha256'], m['size'], state.get('previous_file_id'))
                archived = any(m['local_date'] == manifest['local_date'] and (r.get('archive_file_id') or not r.get('current_file_id'))
                               for _, m, r in entries if r)
                if not archived:
                    asset('archive', manifest['zip_name'], directory / 'snapshot.zip', manifest['sha256'], manifest['size'])
                asset('current', f'CURRENT-{device}.zip', directory / 'snapshot.zip', manifest['sha256'], manifest['size'], state.get('current_file_id'))
                asset('readable', manifest['readable_name'], directory / 'MEMORY.md', manifest['readable_sha256'], manifest['readable_size'], state.get('readable_file_id'))
                transaction = dict(snapshot_id=manifest['snapshot_id'], folder_id=config['folder_id'], uploads=uploads, verified={})
                _atomic_json(directory / 'transaction.json', transaction)
            for asset in transaction['uploads']:
                if _digest(Path(asset['path'])) != asset['sha256']:
                    raise ValueError('snapshot_integrity_failed')
            return {'enabled': True, 'pending': True, 'mode': 'rolling_with_daily_archive', **manifest, **transaction}
        return {'enabled': True, 'pending': False, 'local_changes_pending': self.status()['local_changes_pending']}

    def verify_file(self, snapshot_id, role, file_id, downloaded):
        if not DRIVE_ID_RE.fullmatch(file_id):
            raise ValueError('invalid_drive_file_id')
        with self._locked():
            directory = self._directory(snapshot_id)
            transaction = _json(directory / 'transaction.json', {})
            asset = next((a for a in transaction.get('uploads', []) if a['role'] == role), None)
            if not asset or (directory / 'receipt.json').exists():
                raise ValueError('pending_transaction_required')
            path = Path(downloaded)
            if not path.is_absolute() or not path.is_file() or _reparse(path) or path.resolve() == Path(asset['path']).resolve():
                raise ValueError('independent_download_required')
            if path.stat().st_size != asset['size'] or _digest(path) != asset['sha256']:
                raise ValueError('cloud_download_hash_mismatch')
            asset['file_id'] = file_id
            transaction['verified'][role] = dict(file_id=file_id, sha256=asset['sha256'], downloaded=str(path), verified_at=_now())
            _atomic_json(directory / 'transaction.json', transaction)
            return {'verified': True, 'role': role, 'sha256': asset['sha256']}

    def commit(self, snapshot_id):
        with self._locked():
            entries = self._entries()
            entry = next((e for e in entries if e[1]['snapshot_id'] == snapshot_id), None)
            if entry is None:
                raise ValueError('snapshot_not_found')
            directory, manifest, receipt = entry
            if receipt:
                return {'acknowledged': True, **receipt}
            transaction = _json(directory / 'transaction.json', {})
            if not transaction.get('uploads'):
                raise ValueError('pending_transaction_required')
            for asset in transaction['uploads']:
                checked = transaction.get('verified', {}).get(asset['role'], {})
                if checked.get('sha256') != asset['sha256'] or not checked.get('file_id'):
                    raise ValueError('all_files_must_be_verified')
                path = Path(checked['downloaded'])
                if _digest(path) != asset['sha256'] or _digest(Path(asset['path'])) != asset['sha256']:
                    raise ValueError('verified_file_changed')
            ids = {role: info['file_id'] for role, info in transaction['verified'].items()}
            receipt = dict(snapshot_id=snapshot_id, uploaded_at=_now(), zip_file_id=ids['current'],
                           current_file_id=ids['current'], previous_file_id=ids.get('previous'),
                           archive_file_id=ids.get('archive'), readable_file_id=ids['readable'],
                           sha256=manifest['sha256'], readable_sha256=manifest['readable_sha256'],
                           readable_size=manifest['readable_size'], folder_id=transaction['folder_id'],
                           verification={role: 'independent_download_sha256' for role in ids})
            _atomic_json(directory / 'receipt.json', receipt)
            self._update_state()
            self._prune(self._config()['retention'])
            return {'acknowledged': True, **receipt}

    def _view_state(self, entries):
        state = _json(self.root / 'state.json', {})
        receipts = [receipt for _, _, receipt in entries if receipt]
        if receipts:
            latest = max(receipts, key=lambda receipt: receipt['uploaded_at'])
            # Receipts are authoritative across a crash between the two writes.
            state.update({'last_success': latest['uploaded_at'],
                          'readable_file_id': latest['readable_file_id'],
                          'current_file_id': latest.get('current_file_id'), 'previous_file_id': latest.get('previous_file_id')})
        return state

    def acknowledge(self, snapshot_id, downloaded_zip, zip_file_id, readable_file_id, readable_size):
        if self._config().get('realtime_enabled'):
            raise ValueError('rolling_mode_requires_verify_file_and_commit')
        for value in (zip_file_id, readable_file_id):
            if not isinstance(value, str) or not DRIVE_ID_RE.fullmatch(value):
                raise ValueError('invalid_drive_file_id')
        with self._locked():
            config = self._config()
            if not config['enabled']:
                raise ValueError('cloud_backup_disabled')
            entry = next((e for e in self._entries() if e[1]['snapshot_id'] == snapshot_id), None)
            if entry is None:
                raise ValueError('snapshot_not_found')
            directory, manifest, previous = entry
            downloaded_zip = Path(downloaded_zip)
            if not downloaded_zip.is_absolute() or downloaded_zip.is_symlink() or not downloaded_zip.is_file():
                raise ValueError('downloaded_file_required')
            if downloaded_zip.resolve() == (directory / 'snapshot.zip').resolve():
                raise ValueError('independent_download_required')
            if downloaded_zip.stat().st_size != manifest['size'] or _digest(downloaded_zip) != manifest['sha256']:
                raise ValueError('cloud_download_hash_mismatch')
            if type(readable_size) is not int or readable_size != manifest['readable_size']:
                raise ValueError('readable_size_mismatch')
            if _digest(directory / 'snapshot.zip') != manifest['sha256'] or _digest(directory / 'MEMORY.md') != manifest['readable_sha256']:
                raise ValueError('snapshot_integrity_failed')
            if previous and (previous['zip_file_id'] != zip_file_id or previous['readable_file_id'] != readable_file_id):
                raise ValueError('receipt_conflict')
            # Writing receipt first is safe: prepare can recover cloud IDs from it
            # if the process exits before state.json is updated.
            receipt = previous or {'snapshot_id': snapshot_id, 'uploaded_at': _now(),
                'zip_file_id': zip_file_id, 'readable_file_id': readable_file_id,
                'sha256': manifest['sha256'], 'readable_sha256': manifest['readable_sha256'],
                'readable_size': readable_size, 'folder_id': config['folder_id'],
                'verification': {'zip': 'download_sha256', 'readable': 'remote_size'}}
            _atomic_json(directory / 'receipt.json', receipt)
            self._update_state()
            self._prune(config['retention'])
            return {'acknowledged': True, **receipt}

    def _update_state(self, last_error=None):
        receipts = [(m, r) for _, m, r in self._entries() if r is not None]
        latest = max(receipts, key=lambda pair: pair[1]['uploaded_at']) if receipts else None
        state = {'format': FORMAT, 'last_error': last_error}
        if latest:
            manifest, receipt = latest
            state.update({'last_success': receipt['uploaded_at'], 'latest_snapshot_id': manifest['snapshot_id'],
                          'zip_file_id': receipt['zip_file_id'], 'readable_file_id': receipt['readable_file_id'],
                          'sha256': manifest['sha256'], 'records': manifest['records'],
                          'current_file_id': receipt.get('current_file_id'), 'previous_file_id': receipt.get('previous_file_id')})
        _atomic_json(self.root / 'state.json', state)

    def fail(self, snapshot_id, error='upload_failed'):
        if not isinstance(error, str) or not re.fullmatch(r'[a-z0-9_-]{1,80}', error):
            raise ValueError('error_code_required_no_urls_or_credentials')
        with self._locked():
            entry = next((e for e in self._entries() if e[1]['snapshot_id'] == snapshot_id), None)
            if entry is None or entry[2] is not None:
                raise ValueError('pending_snapshot_required')
            directory, _, _ = entry
            previous = _json(directory / 'failure.json', {})
            _atomic_json(directory / 'failure.json', {'snapshot_id': snapshot_id, 'last_attempt': _now(),
                         'attempts': previous.get('attempts', 0) + 1, 'error': error})
            self._update_state({'snapshot_id': snapshot_id, 'code': error, 'time': _now()})
            return {'pending': True, 'error': error}

    def _prune(self, retention):
        # Pending snapshots never expire. Cloud retention is a separate policy.
        acknowledged = [directory for directory, _, receipt in self._entries() if receipt]
        for directory in acknowledged[:-retention]:
            checked = self._directory(directory.name)
            if checked.resolve().parent != self.snapshots.resolve():
                raise ValueError('unsafe_retention_path')
            self._remove_directory(checked)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('prepare')
    verify = sub.add_parser('verify-file')
    verify.add_argument('--snapshot', required=True)
    verify.add_argument('--role', required=True, choices=['current', 'previous', 'readable', 'archive'])
    verify.add_argument('--file-id', required=True)
    verify.add_argument('--downloaded', required=True)
    commit = sub.add_parser('commit')
    commit.add_argument('--snapshot', required=True)
    ack = sub.add_parser('ack')
    ack.add_argument('--snapshot', required=True)
    ack.add_argument('--downloaded', required=True)
    ack.add_argument('--zip-file-id', required=True)
    ack.add_argument('--readable-file-id', required=True)
    ack.add_argument('--readable-size', type=int, required=True)
    failure = sub.add_parser('fail')
    failure.add_argument('--snapshot', required=True)
    failure.add_argument('--error', default='upload_failed')
    args = parser.parse_args()
    queue = CloudBackup(args.data_root)
    try:
        if args.command == 'verify-file':
            result = queue.verify_file(args.snapshot, args.role, args.file_id, args.downloaded)
        elif args.command == 'commit':
            result = queue.commit(args.snapshot)
        elif args.command == 'ack':
            result = queue.acknowledge(args.snapshot, args.downloaded, args.zip_file_id, args.readable_file_id, args.readable_size)
        elif args.command == 'fail':
            result = queue.fail(args.snapshot, args.error)
        else:
            result = getattr(queue, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        # Error type/code only: cloud errors must not persist signed URLs/tokens.
        code = error_code(exc)
        print(json.dumps({'error': code}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
