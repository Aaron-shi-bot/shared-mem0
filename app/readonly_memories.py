"""User-selected device snapshots, isolated from local originals and cloud exports."""
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import zipfile
from datetime import datetime, timezone

from cloud_backup import _atomic_json
from migration_archive import open_snapshot
from memory_lifecycle import superseded, validate_lifecycle


class ReadonlyMemories:
    def __init__(self, data_root):
        self.root = Path(data_root) / 'imports' / 'readonly'
        self.lock = threading.RLock()

    def records(self):
        with self.lock:
            return [{k: v for k, v in row.items() if k != 'vector'}
                    for path in sorted(self.root.glob('*.json'))
                    for row in json.loads(path.read_text(encoding='utf-8'))['records']]

    def preview(self, path, device, project_map=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', device):
            raise ValueError('invalid_source_device')
        raw, manifest = open_snapshot(Path(path))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            doc = json.loads(z.read('memories.json'))
            registry = json.loads(z.read('automation/projects.json')) if 'automation/projects.json' in z.namelist() else {}
        validate_lifecycle(doc['records'])
        installed_path = self.root / (device + '.json')
        old = json.loads(installed_path.read_text(encoding='utf-8')) if installed_path.exists() else {}
        prior = {r['metadata'].get('origin_id', r['id']): r for r in old.get('records', [])}
        incoming = {r['id']: r for r in doc['records']}
        previous = old.get('preview', {})
        def timestamp(value):
            try: return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, AttributeError): return 0
        mapping = previous.get('project_map', {}) if project_map is None else project_map
        def imported_metadata(record):
            meta = dict(record['metadata'])
            meta.update(source_device=device, readonly=True, origin_id=record['id'], origin_project=meta['project'])
            if meta.get('superseded_by'):
                meta['origin_superseded_by'] = meta['superseded_by']
                meta['superseded_by'] = device + ':' + meta['superseded_by']
            meta['project'] = mapping.get(meta['project'], meta['project'])
            return meta
        changed = sum(prior[rid]['memory'] != r['memory'] or prior[rid]['metadata'] != imported_metadata(r)
                      for rid, r in incoming.items() if rid in prior)
        return raw, doc, {'device': device, 'sha256': hashlib.sha256(raw).hexdigest(),
                         'records': len(doc['records']), 'created_at': doc.get('created_at'),
                         'projects': sorted({r['metadata']['project'] for r in doc['records']}),
                         'project_labels': {k: v.get('label', k) for k, v in registry.get('projects', {}).items()},
                         'superseded': sum(superseded(r) for r in doc['records']),
                         'project_map': mapping, 'previous_sha256': previous.get('sha256'),
                         'previous_created_at': previous.get('created_at'),
                         'older_than_installed': bool(previous and timestamp(doc.get('created_at')) < timestamp(previous.get('created_at'))),
                         'changes': {'added': len(incoming.keys() - prior.keys()), 'changed': changed,
                                     'removed': len(prior.keys() - incoming.keys()),
                                     'unchanged': len(incoming.keys() & prior.keys()) - changed}}

    def install(self, path, device, expected_sha256, embedder=None, project_map=None, expected_previous_sha256=None):
        with self.lock:
            _, doc, preview = self.preview(path, device, project_map)
            if preview['sha256'] != expected_sha256:
                raise ValueError('preview_changed')
            if expected_previous_sha256 is not None and (preview['previous_sha256'] or '') != expected_previous_sha256:
                raise ValueError('installed_source_changed_preview_again')
            if preview['older_than_installed']:
                raise ValueError('older_snapshot_cannot_replace_newer_source')
            project_map = preview['project_map']
            for source, target in project_map.items():
                if source not in preview['projects'] or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', target):
                    raise ValueError('invalid_project_mapping')
                if (source == 'global') != (target == 'global'):
                    raise ValueError('global_project_mapping_forbidden')
            rows = []
            for record in doc['records']:
                meta = dict(record['metadata'])
                meta.update(source_device=device, readonly=True, origin_id=record['id'],
                            origin_project=meta['project'])
                if meta.get('superseded_by'):
                    meta['origin_superseded_by'] = meta['superseded_by']
                    meta['superseded_by'] = device + ':' + meta['superseded_by']
                meta['project'] = project_map.get(meta['project'], meta['project'])
                row = {'id': device + ':' + record['id'], 'memory': record['memory'], 'metadata': meta}
                if embedder is not None:
                    row['vector'] = embedder.embed(record['memory'], 'add')
                rows.append(row)
            # One atomic partition swap; a failed embedding never installs half a source.
            preview.update(installed_at=datetime.now(timezone.utc).isoformat(), selected_path=str(Path(path).resolve()))
            _atomic_json(self.root / (device + '.json'), {'preview': preview, 'records': rows})
            return {**preview, 'imported': True, 'readonly': True}

    def sources(self):
        with self.lock:
            return [json.loads(p.read_text(encoding='utf-8'))['preview'] for p in sorted(self.root.glob('*.json'))]

    def search(self, vector, filters, limit, include_history=False):
        import numpy as np
        results = []
        query = np.array(vector, dtype=float)
        qnorm = np.linalg.norm(query)
        with self.lock:
            for path in sorted(self.root.glob('*.json')):
                for row in json.loads(path.read_text(encoding='utf-8'))['records']:
                    meta = row['metadata']
                    if any(meta.get(k) != v for k, v in filters.items() if k != 'user_id'):
                        continue
                    if not include_history and superseded(row):
                        continue
                    v = np.array(row['vector'], dtype=float)
                    norm = qnorm * np.linalg.norm(v)
                    score = float(np.dot(query, v) / norm) if norm else 0.0
                    if score >= .05:
                        results.append({k: val for k, val in row.items() if k != 'vector'} | {'score': score})
        return sorted(results, key=lambda r: r['score'], reverse=True)[:limit]
