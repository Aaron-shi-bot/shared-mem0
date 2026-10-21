"""Backup timing derived from the last durable local snapshot, not process uptime."""
import stat
import time
from datetime import datetime, timezone
from pathlib import Path


def snapshot_mtime(path):
    """Missing, empty, or non-file paths are not successful snapshots."""
    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return None
    return info.st_mtime if stat.S_ISREG(info.st_mode) and info.st_size > 0 else None


def seconds_until_backup(path, interval_seconds, current_time=None):
    current_time = time.time() if current_time is None else current_time
    interval = float(interval_seconds)
    if interval <= 0:
        raise ValueError('Backup interval must be positive')
    modified = snapshot_mtime(path)
    # A future timestamp cannot be trusted after a wall-clock correction.
    if modified is None or modified > current_time:
        return 0.0
    return max(0.0, modified + interval - current_time)


def stored_backup_status(local, shared=None):
    result = {'last_local': None, 'last_shared': None, 'error': None}
    errors = []
    for field, path in (('last_local', local), ('last_shared', shared)):
        if path is None:
            continue
        try:
            modified = snapshot_mtime(path)
            if modified is not None:
                result[field] = datetime.fromtimestamp(modified, timezone.utc).isoformat()
        except OSError as exc:
            errors.append(f'{field}: {type(exc).__name__}: {exc}')
    result['error'] = '; '.join(errors) or None
    return result


def run_backup_schedule(local, interval_seconds, create_backup, stop_event, on_error,
                        clock=time.time, poll_seconds=60, retry_seconds=300):
    """Recheck after manual backups; retry failures with an interruptible delay."""
    while not stop_event.is_set():
        try:
            delay = seconds_until_backup(local, interval_seconds, clock())
            if delay <= 0:
                create_backup(force=False)
                delay = seconds_until_backup(local, interval_seconds, clock())
            # A callback that did not create a usable snapshot must not spin.
            wait = min(delay, poll_seconds) if delay > 0 else retry_seconds
        except Exception as exc:
            on_error(exc)
            wait = retry_seconds
        stop_event.wait(max(1, wait))
