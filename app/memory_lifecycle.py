"""Explicit decision replacement and non-destructive portable record restore."""
import hashlib
import json
import time
from datetime import datetime, timezone

LIFECYCLE = ('memory_status', 'superseded_by', 'superseded_at',
             'supersession_reason', 'supersession_source_agent', 'supersession_unresolved')


def superseded(record):
    return record.get('metadata', {}).get('memory_status') == 'superseded'


def namespace(meta):
    return (meta.get('scope', 'project'), meta.get('project', ''),
            meta.get('task_id', '') if meta.get('scope') == 'task' else '')


def replacement_patch(records, memory_id, replacement_id, reason, agent):
    by_id = {r['id']: r for r in records}
    if memory_id not in by_id or replacement_id not in by_id:
        raise ValueError('memory_not_found')
    old, new = by_id[memory_id], by_id[replacement_id]
    if memory_id == replacement_id or namespace(old['metadata']) != namespace(new['metadata']):
        raise ValueError('replacement_scope_mismatch')
    if superseded(new):
        raise ValueError('replacement_must_be_active')
    if superseded(old):
        if old['metadata'].get('superseded_by') == replacement_id:
            return None
        raise ValueError('already_superseded_by_another_record')
    return dict(memory_status='superseded', superseded_by=replacement_id,
                superseded_at=datetime.now(timezone.utc).isoformat(),
                supersession_reason=reason, supersession_source_agent=agent,
                supersession_unresolved=False)


def validate_lifecycle(records):
    """Validate the complete graph before any restore writes."""
    ids = [r.get('id') for r in records]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid_or_duplicate_record_ids')
    by_id = {r['id']: r for r in records}
    for record in records:
        meta = record.get('metadata', {})
        if meta.get('memory_status', 'active') not in ('active', 'superseded'):
            raise ValueError('invalid_memory_status')
        if not superseded(record):
            continue
        seen = {record['id']}
        current = record
        while superseded(current):
            target = current['metadata'].get('superseded_by')
            if not target:
                if current['metadata'].get('supersession_unresolved'):
                    break
                raise ValueError('superseded_record_missing_replacement')
            if target in seen:
                raise ValueError('replacement_cycle')
            seen.add(target)
            if target not in by_id:
                if current['metadata'].get('supersession_unresolved'):
                    break
                raise ValueError('replacement_not_in_snapshot')
            new = by_id[target]
            if namespace(current['metadata']) != namespace(new['metadata']):
                raise ValueError('replacement_scope_mismatch')
            current = new


def restore_records(engine, user, existing, incoming):
    """Merge without overwriting existing facts. New invalidated facts start invalidated.

    Partial failure is recoverable: origin IDs and content identities make repeats
    idempotent; unresolved edges stay hidden until all replacement IDs are known.
    """
    validate_lifecycle(incoming)
    identities = {(namespace(r['metadata']), r['memory'].strip()): r for r in existing}
    autos = {r['metadata']['auto_key']: r for r in existing if r['metadata'].get('auto_key')}
    result = dict(total=len(incoming), restored=0, skipped=0, updated=0,
                  failed=0, conflicts=[], errors=[], complete=True, pending=False, id_map={})
    new_records = []
    started = time.monotonic()
    for record in incoming:
        meta = dict(record['metadata'])
        text = record['memory'].strip()
        old = autos.get(meta.get('auto_key')) or identities.get((namespace(meta), text))
        if old:
            if old['memory'].strip() != text or namespace(old['metadata']) != namespace(meta):
                result['conflicts'].append({'id': record['id'], 'resolution': 'kept_existing'})
                result['complete'] = False
                continue
            result['id_map'][record['id']] = old['id']
            result['skipped'] += 1
            # Restoring an old snapshot must never invalidate a current local fact.
            if superseded(record) != superseded(old):
                result['conflicts'].append({'id': record['id'], 'kind': 'lifecycle', 'resolution': 'kept_existing'})
                result['complete'] = False
            elif (superseded(old) and old['metadata'].get('supersession_unresolved')
                  and old['metadata'].get('restored_origin_id') == record['id']):
                new_records.append((record, old['id']))
            continue
        if result['restored'] >= 25 or time.monotonic() - started > 20:
            result.update(complete=False, pending=True)
            break
        try:
            for field in ('user_id', 'agent_id', 'run_id', 'hash', 'text_lemmatized'):
                meta.pop(field, None)
            if superseded(record):
                meta['superseded_by'] = ''
                meta['supersession_unresolved'] = True
                meta['restore_replacement_origin_id'] = record['metadata'].get('superseded_by', '')
            meta['restored_origin_id'] = record['id']
            filters = {'user_id': user, 'scope': meta.get('scope', 'project'), 'project': meta['project']}
            if filters['scope'] == 'task':
                filters['task_id'] = meta['task_id']
            meta['content_hash'] = hashlib.sha256((json.dumps(filters, sort_keys=True) + text).encode()).hexdigest()
            added = engine.add(text, user_id=user, infer=False, metadata=meta,
                               run_id=hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest())
            target_id = added['results'][0]['id']
            result['id_map'][record['id']] = target_id
            result['restored'] += 1
            local = {'id': target_id, 'memory': text, 'metadata': meta}
            identities[(namespace(meta), text)] = local
            if meta.get('auto_key'):
                autos[meta['auto_key']] = local
            new_records.append((record, target_id))
        except Exception as exc:
            result['failed'] += 1
            result['errors'].append({'id': record['id'], 'code': type(exc).__name__})
            result['complete'] = False
            break
    for record, local_id in new_records:
        if not superseded(record):
            continue
        target = result['id_map'].get(record['metadata'].get('superseded_by'))
        if target:
            patch = {k: record['metadata'][k] for k in LIFECYCLE if k in record['metadata']}
            patch.update(superseded_by=target, supersession_unresolved=False)
            engine.update(local_id, metadata=patch)
        else:
            result['complete'] = False
            result['conflicts'].append({'id': record['id'], 'kind': 'unresolved_replacement'})
    return result
