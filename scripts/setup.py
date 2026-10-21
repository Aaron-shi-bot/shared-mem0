"""Create local configuration without downloading software or modifying clients.

Run with the Python interpreter that will also run Shared Mem0. Secrets and
existing settings are never overwritten. Optional collectors and cloud backup
start disabled; their external integrations require separate configuration.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]


def default_data_root() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'SharedMem0'


def write_new(path: Path, body: str) -> bool:
    """Exclusive creation also protects secrets against concurrent setup runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Existing file is empty or invalid; inspect it manually: {path}')
        return False
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(body)
    return True


def write_json(path: Path, value: dict) -> bool:
    return write_new(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def initialize(config_dir: Path, data_root: Path | None = None,
               port: int | None = None, device_id: str | None = None) -> dict:
    config_dir = config_dir.expanduser().resolve()
    settings_path = config_dir / 'settings.json'
    if port is not None and not 1024 <= port <= 65535:
        raise ValueError('--port must be between 1024 and 65535')
    if device_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', device_id):
        raise ValueError('--device-id must contain 1-80 letters, digits, dots, underscores or hyphens')
    data_root = data_root.expanduser().resolve() if data_root is not None else None
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
        actual_data = Path(settings['data_root']).expanduser().resolve()
        for requested, actual, name in ((data_root, actual_data, '--data-root'),
                                         (port, settings['port'], '--port'),
                                         (device_id, settings['device_id'], '--device-id')):
            if requested is not None and requested != actual:
                raise ValueError(f'{name} differs from existing settings; edit or migrate that installation explicitly')
        if type(settings['port']) is not int or not 1024 <= settings['port'] <= 65535:
            raise ValueError('Existing service port is invalid')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', settings['device_id']):
            raise ValueError('Existing device ID is invalid')
        data_root = actual_data
        created_settings = False
    else:
        data_root = data_root or default_data_root().expanduser().resolve()
        settings = {
            'data_root': data_root.as_posix(),
            'model_cache_root': data_root.as_posix(),
            'user_id': 'shared-mem0-user',
            'device_id': device_id or 'device-' + secrets.token_hex(6),
            'port': port if port is not None else 8766,
            'embedding_model': 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
            'llm': {'enabled': False, 'model': '', 'base_url': '', 'api_key': ''},
            'backup': {'enabled': True, 'interval_seconds': 86400,
                       'keep_versions': 48, 'shared_folder': '', 'share_root': ''},
        }
        created_settings = write_json(settings_path, settings)
        if not created_settings:
            # A concurrent setup has selected an installation. Never generate
            # its keys or state using this process's different settings.
            raise ValueError('Settings were created concurrently; rerun setup')
    for relative in ('data/qdrant', 'data/mem0', 'exports/readable', 'native',
                     'automation/watchdog', 'backups/portable', 'backups/client-configs',
                     'backups/google-drive/snapshots', 'imports/readonly',
                     'logs/service', 'logs/watchdog', 'cache/models', 'cache/huggingface'):
        (data_root / relative).mkdir(parents=True, exist_ok=True)
    api_created = write_new(config_dir / 'local-api.key', secrets.token_urlsafe(48) + '\n')
    recovery_created = write_new(config_dir / 'recovery.key', base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii') + '\n')
    write_json(data_root / 'automation' / 'config.json', {
        'enabled': False, 'interval_seconds': 60, 'quiet_seconds': 90,
        'chunk_chars': 16000, 'summarizer': 'GPT task configured separately',
        'lease_seconds': 1800,
    })
    write_json(data_root / 'automation' / 'projects.json', {'aliases': [], 'projects': {}})
    write_json(data_root / 'backups' / 'google-drive' / 'config.json', {
        'enabled': False, 'folder_id': '', 'retention': 30,
        'realtime_enabled': False, 'upload_mode': 'codex', 'background_upload_enabled': False,
    })
    return {
        'settings': str(settings_path), 'data_root': str(data_root),
        'device_id': settings['device_id'], 'dashboard': f"http://127.0.0.1:{settings['port']}",
        'created': {'settings': created_settings, 'local_api_key': api_created,
                    'recovery_key': recovery_created},
        'note': 'No dependencies downloaded, service started, clients changed, or scheduled tasks installed. '
                'Initial collection, LLM and cloud backup are disabled; existing configuration is preserved.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, help='Default: %%LOCALAPPDATA%%/SharedMem0')
    parser.add_argument('--port', type=int, help='Initial loopback port; default 8766')
    parser.add_argument('--device-id', help='Stable ID for this device; default: randomly generated')
    parser.add_argument('--config-dir', type=Path, default=ROOT / 'config',
                        help='Alternate configuration directory for isolated checks (default: repository/config)')
    args = parser.parse_args()
    try:
        result = initialize(args.config_dir, args.data_root, args.port, args.device_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
