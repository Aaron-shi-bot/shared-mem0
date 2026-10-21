"""Rebuildable full-text passage index. Originals and lifecycle remain authoritative."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata

import numpy as np
from tokenizers import Tokenizer
from memory_lifecycle import superseded

VERSION = 'passages-v1'


def terms(text):
    text = unicodedata.normalize('NFKC', text).lower()
    result = re.findall(r'[a-z0-9_]+', text)
    result.extend(part for token in list(result) if '_' in token for part in token.split('_') if part)
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.extend(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            result.append(run)
    return result


def passages(text, tokenizer, max_tokens):
    """Every non-whitespace character is indexed; no summarization or rewriting."""
    output = []
    for match in re.finditer(r'[^\n。！？!?]+[。！？!?]*|[。！？!?]+', text):
        unit = match.group().strip()
        if not unit:
            continue
        offsets = [(a, b) for a, b in tokenizer.encode(unit).offsets if b > a]
        if not offsets:
            output.append(unit)
            continue
        start = 0
        budget = max(4, min(96, max_tokens - 8))
        while start < len(offsets):
            end = min(start + budget, len(offsets))
            left = 0 if start == 0 else offsets[start][0]
            right = len(unit) if end == len(offsets) else offsets[end - 1][1]
            piece = unit[left:right]
            while len(tokenizer.encode(piece).ids) > max_tokens:
                end -= 1
                if end <= start:
                    raise ValueError('single_token_cannot_fit')
                right = offsets[end - 1][1]
                piece = unit[left:right]
            output.append(piece)
            if end == len(offsets):
                break
            start = max(start + 1, end - min(16, (end - start) // 4))
    return output or [text]


def role(record, targets):
    md = record.get('metadata', {})
    if superseded(record):
        return 'superseded'
    if record['id'] in targets or md.get('record_kind') == 'decision':
        return 'confirmed_decision'
    if md.get('record_kind') == 'historical' or md.get('automatic'):
        return 'historical_context'
    if md.get('scope') == 'task' or md.get('record_kind') == 'handoff':
        return 'handoff'
    return 'reference'


class PassageIndex:
    def __init__(self, data_root, embedder):
        self.path = Path(data_root) / 'data' / 'retrieval.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        actual = embedder.dense_model.model.tokenizer
        self.max_tokens = actual.truncation['max_length'] if actual.truncation else 128
        self.tokenizer = Tokenizer.from_str(actual.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.signature = hashlib.sha256((VERSION + actual.to_str() + str(embedder.config.model)).encode()).hexdigest()
        self.lock = threading.RLock()
        self._rows = []
        self._signature = None
        self._status = {'ready': False, 'version': VERSION, 'max_tokens': self.max_tokens}
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,hash TEXT,body TEXT);'
                             'CREATE TABLE IF NOT EXISTS passages(id TEXT,position INTEGER,text TEXT,vector BLOB,PRIMARY KEY(id,position));')
            saved = db.execute("SELECT value FROM settings WHERE key='signature'").fetchone()
            if saved and saved[0] != self.signature:
                db.execute('DELETE FROM passages')
                db.execute('DELETE FROM records')
            db.execute("INSERT OR REPLACE INTO settings VALUES('signature',?)", (self.signature,))

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def status(self):
        return dict(self._status)

    def sync(self, records):
        with self.lock:
            canonical = json.dumps(records, ensure_ascii=False, sort_keys=True)
            signature = hashlib.sha256(canonical.encode()).hexdigest()
            if signature == self._signature:
                return self.status()
            self._status.update(ready=False, building=True, error=None)
            try:
                with self.db() as db:
                    existing = dict(db.execute('SELECT id,hash FROM records'))
                    prepared = []
                    for row in records:
                        body_hash = hashlib.sha256(row['memory'].encode()).hexdigest()
                        if existing.get(row['id']) != body_hash:
                            prepared.append((row, body_hash, passages(row['memory'], self.tokenizer, self.max_tokens)))
                    texts = [s.replace('\n', ' ') for _, _, chunks in prepared for s in chunks]
                    vectors = list(self.embedder.dense_model.embed(texts, batch_size=32)) if texts else []
                    if len(vectors) != len(texts):
                        raise ValueError('incomplete_passage_embeddings')
                    cursor = iter(vectors)
                    for row, body_hash, chunks in prepared:
                        db.execute('DELETE FROM passages WHERE id=?', (row['id'],))
                        for pos, chunk in enumerate(chunks):
                            vector = np.asarray(next(cursor), dtype=np.float32)
                            if not np.isfinite(vector).all():
                                raise ValueError('nonfinite_embedding')
                            db.execute('INSERT INTO passages VALUES(?,?,?,?)', (row['id'], pos, chunk, vector.tobytes()))
                    ids = {r['id'] for r in records}
                    for rid in existing.keys() - ids:
                        db.execute('DELETE FROM passages WHERE id=?', (rid,))
                        db.execute('DELETE FROM records WHERE id=?', (rid,))
                    for row in records:
                        db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)', (row['id'], hashlib.sha256(row['memory'].encode()).hexdigest(), json.dumps(row, ensure_ascii=False)))
                    loaded = list(db.execute('SELECT id,position,text,vector FROM passages ORDER BY id,position'))
                self.records = {r['id']: r for r in records}
                self.targets = {r['metadata'].get('superseded_by') for r in records if superseded(r)}
                self._rows = [(rid, pos, txt, np.frombuffer(vec, dtype=np.float32), Counter(terms(txt))) for rid, pos, txt, vec in loaded]
                self._signature = signature
                self._status.update(ready=True, building=False, records=len(records), passages=len(loaded),
                                    indexed_at=datetime.now(timezone.utc).isoformat(), updated_records=len(prepared))
                return self.status()
            except Exception as exc:
                self._status.update(ready=False, building=False, error=type(exc).__name__)
                raise

    def search(self, query, filters, limit=8, include_history=False, include_tasks=False, include_imported=True):
        with self.lock:
            allowed = {}
            for rid, row in self.records.items():
                md = row['metadata']
                if md.get('readonly') and not include_imported:
                    continue
                matches = all(md.get(k) == v for k, v in filters.items() if k not in ('user_id', 'scope'))
                scopes = [filters['scope']]
                if include_tasks and filters['scope'] == 'project':
                    scopes.append('task')
                if matches and md.get('scope', 'project') in scopes and (include_history or not superseded(row)):
                    allowed[rid] = row
            selected = [x for x in self._rows if x[0] in allowed]
            if not selected:
                return []
            qpieces = passages(query, self.tokenizer, self.max_tokens)
            qvectors = np.asarray(list(self.embedder.dense_model.embed(qpieces, batch_size=32)), dtype=np.float32)
            matrix = np.vstack([x[3] for x in selected])
            norms = np.maximum(np.linalg.norm(matrix, axis=1), 1e-12)
            qnorms = np.maximum(np.linalg.norm(qvectors, axis=1), 1e-12)
            semantic = ((matrix @ qvectors.T) / norms[:, None] / qnorms).max(axis=1)
            qterms = set(terms(query))
            stop = {'现在','当前','什么','哪些','如何','怎样','是否','已经','应该','还是','要求','用户','问题','怎么','进行','使用','应该','究竟','没有','多少'}
            qterms -= stop
            n = len(selected)
            df = Counter(term for x in selected for term in x[4].keys() & qterms)
            avg_len = sum(sum(x[4].values()) for x in selected) / n
            idf = {term: math.log(1 + (n - count + .5) / (count + .5)) for term, count in df.items()}
            lexical = []
            for *_, counts in selected:
                length = sum(counts.values())
                value = sum(idf[t] * counts[t] * 2.2 / (counts[t] + 1.2 * (.25 + .75 * length / max(avg_len, 1))) for t in qterms & counts.keys())
                lexical.append(value)
            max_lexical = max(lexical, default=0) or 1
            parent_terms = {rid: set(terms(row['memory'])) for rid, row in allowed.items()}
            identifiers = {t for t in qterms if re.fullmatch(r'[a-z][a-z0-9_]{2,}', t) and t in df
                           and sum(t in values for values in parent_terms.values()) <= max(1, len(allowed) * .35)}
            results = {}
            historical_query = include_history or bool(re.search(r'以前|当时|历史|最初|原先|曾经|为什么.*改|为何.*改', query))
            for idx, (rid, pos, txt, _, counts) in enumerate(selected):
                sem = float(semantic[idx])
                lex = lexical[idx] / max_lexical
                if sem < .18 and not lexical[idx]:
                    continue
                row = allowed[rid]
                record_role = role(row, self.targets)
                score = .72 * sem + .28 * lex
                if identifiers:
                    matched = len(identifiers & counts.keys()) / len(identifiers)
                    score += .18 * matched
                    if not identifiers.intersection(parent_terms[rid]):
                        score -= .18
                if not historical_query:
                    if record_role == 'confirmed_decision' and sem >= .3:
                        score += .16
                    elif record_role == 'historical_context':
                        score -= .06
                previous = results.get(rid)
                if previous and previous['score'] >= score:
                    continue
                results[rid] = {**row, 'score': round(score, 6), 'retrieval': {
                    'role': record_role, 'matched_passage': txt, 'passage': pos,
                    'semantic': round(sem, 6), 'lexical': round(lex, 6),
                    'source_time': row['metadata'].get('saved_at'),
                    'warning': '历史资料，请结合现行决定核实' if record_role == 'historical_context' else None}}
            # Copies from other devices stay separately stored, but do not crowd out context.
            output = []
            seen = {}
            for row in sorted(results.values(), key=lambda r: r['score'], reverse=True):
                identity = (row['memory'].strip(), row['metadata'].get('memory_status', 'active'))
                if identity in seen:
                    seen[identity].setdefault('also_available_from', []).append(row['metadata'].get('source_device', 'local'))
                    continue
                seen[identity] = row
                output.append(row)
            return output[:limit]
