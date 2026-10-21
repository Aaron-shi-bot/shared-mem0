"""Portable memory-only assets and consistent SQLite snapshots.

Called by server.create_backup while its backup lock is held. Account-native
memory mirrors stay under the configured data root. No raw transcript discovery or
credential/config copying happens here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _inside(path: Path, root: Path):
    return path.resolve().is_relative_to(root.resolve())


def refresh_native_mirrors(data_root: Path, home: Path | None = None) -> dict:
    """Refresh WorkBuddy root/account MEMORY.md and Claude project memory/*.md.

    Never relocates or changes source files. The manifest describes current
    sources; older mirrored files are retained instead of silently deleted.
    Claude CLAUDE.md is instruction/configuration and is intentionally excluded.
    """
    data_root, home = Path(data_root), Path(home or Path.home())
    native = data_root / 'native'
    candidates = []
    workbuddy = home / '.workbuddy'
    legacy = workbuddy / 'MEMORY.md'
    if legacy.is_file():
        candidates.append((legacy, native / 'workbuddy' / 'legacy' / 'MEMORY.md', workbuddy))
    if workbuddy.is_dir():
        for account in sorted(workbuddy.glob('user-*')):
            source = account / 'MEMORY.md'
            if account.is_dir() and source.is_file():
                candidates.append((source, native / 'workbuddy' / 'accounts' / account.name / 'MEMORY.md', workbuddy))
    # Claude Code supports a user-chosen config root. Follow it only for its
    # native memory subdirectories; never mirror settings or credentials.
    claude_config = os.environ.get('CLAUDE_CONFIG_DIR')
    claude_home = Path(claude_config).expanduser() if claude_config else home / '.claude'
    claude = claude_home / 'projects'
    if claude.is_dir():
        for memory_dir in sorted(claude.glob('*/memory')):
            if memory_dir.is_dir():
                for source in sorted(memory_dir.rglob('*.md')):
                    if '.git' not in source.parts:
                        target = native / 'claude' / 'projects' / memory_dir.parent.name / 'memory' / source.relative_to(memory_dir)
                        candidates.append((source, target, claude))
    result = {'updated_at': _now(), 'mirrored': 0, 'unchanged': 0, 'files': [], 'errors': []}
    for source, target, allowed in candidates:
        try:
            if not _inside(source, allowed) or not _inside(target, native):
                raise ValueError('Memory path escapes its expected directory')
            body = source.read_bytes()
            changed = not target.exists() or target.read_bytes() != body
            if changed:
                _atomic_write(target, body)
            result['mirrored' if changed else 'unchanged'] += 1
            result['files'].append({'source': str(source), 'target': str(target.relative_to(data_root)),
                                    'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                                    'source_mtime_ns': source.stat().st_mtime_ns})
        except Exception as exc:
            result['errors'].append({'path': str(source), 'error': f'{type(exc).__name__}: {exc}'})
    _atomic_write(native / 'mirror-manifest.json', json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
    return result


def sqlite_snapshot(path: Path, work_root: Path, timeout: float = 30.0) -> bytes:
    """Read a live source with mode=ro; emit a standalone non-WAL database."""
    path, work_root = Path(path), Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='snapshot-', suffix='.sqlite', dir=work_root)
    os.close(fd)
    source = destination = None
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite snapshot exceeded its time limit')

    try:
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        source.execute('PRAGMA query_only=ON')
        destination = sqlite3.connect(temporary)
        source.backup(destination, pages=128, progress=progress, sleep=0.05)
        destination.execute('PRAGMA journal_mode=DELETE')
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite snapshot integrity check failed')
        destination.close()
        destination = None
        return Path(temporary).read_bytes()
    finally:
        if source is not None:
            source.close()
        if destination is not None:
            destination.close()
        Path(temporary).unlink(missing_ok=True)


def _excluded(path: Path):
    return (any(part in ('.git', '__pycache__') or '.pi-hermes-locks' in part for part in path.parts)
            or path.name.endswith(('-wal', '-shm', '.lock', '.tmp', '.bak'))
            or '.recovery-' in path.name or path.name == 'user-memory-snapshot.md')


def add_memory_assets(archive, data_root: Path, app_root: Path | None = None,
                      home: Path | None = None) -> dict:
    """Write native/ and automation/ assets into an already-open ZipFile.

    Includes current native memory, the legacy Pi native memory DB, and the
    automatic collector's state/config/project map. Never includes application
    credentials, raw session directories, live Qdrant, logs, or recovery keys.
    Errors are returned and persisted in the archive manifest; callers should
    surface them in backup status instead of claiming complete coverage.
    """
    data_root = Path(data_root)
    mirrored = refresh_native_mirrors(data_root, home)
    result = {'updated_at': _now(), 'files': 0, 'sqlite_snapshots': 0,
              'native_mirrors': mirrored, 'errors': list(mirrored['errors'])}
    native = data_root / 'native'
    candidates = []
    if native.is_dir():
        candidates.extend((p, 'native/' + p.relative_to(native).as_posix())
                          for p in sorted(native.rglob('*'))
                          if p.is_file() and not _excluded(p)
                          and p.suffix.lower() in ('.md', '.txt', '.json', '.db', '.sqlite', '.sqlite3'))
    for filename in ('config.json', 'projects.json', 'state.sqlite'):
        path = data_root / 'automation' / filename
        if path.is_file():
            candidates.append((path, 'automation/' + filename))
    for path, name in candidates:
        try:
            if not _inside(path, data_root):
                raise ValueError('Asset path escapes memory root')
            if path.suffix.lower() in ('.db', '.sqlite', '.sqlite3'):
                body = sqlite_snapshot(path, data_root / 'backups' / 'work')
                result['sqlite_snapshots'] += 1
            else:
                body = path.read_bytes()
            archive.writestr(name, body)
            result['files'] += 1
        except Exception as exc:
            result['errors'].append({'path': str(path), 'error': f'{type(exc).__name__}: {exc}'})
    archive.writestr('memory-assets-manifest.json', json.dumps(result, ensure_ascii=False, indent=2))
    return result
