"""Read-only, bounded parsers for local agent transcripts.

Transcript text is untrusted data. This module never executes it, calls a model,
writes to a source, or copies raw transcripts. Descriptors use a stat fingerprint
(path, byte size, mtime_ns), not a claim of a content hash. Parsing reports every
limit, malformed line and unsupported compressed input; callers must not mark an
incomplete parse as fully imported. ``project_hint`` is a path hint, not a stable
cross-computer project ID; the caller owns that mapping.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Iterator, Mapping

CLIENTS = ("codex", "workbuddy", "pi", "claude")
DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 1_200_000
DEFAULT_MAX_MESSAGE_CHARS = 80_000
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024


def _iso_timestamp(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value,
                                          timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return value if isinstance(value, str) else None


def _stat_fingerprint(path: Path):
    stat = path.stat()
    payload = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return stat, hashlib.sha256(payload.encode("utf-8")).hexdigest()


@contextmanager
def _open_source(path: Path):
    with path.open("rb") as raw:
        if path.name.endswith(".zst"):
            try:
                import zstandard
            except ImportError as exc:
                raise RuntimeError("zstandard_dependency_missing") from exc
            with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                with io.BufferedReader(stream) as buffered:
                    yield buffered
        else:
            yield raw


def _bounded_lines(stream, max_bytes, max_line_bytes, stats):
    """Yield complete bounded lines and drain oversized lines without retaining them."""
    used = 0
    line_no = 0
    while used < max_bytes:
        budget = min(max_line_bytes + 1, max_bytes - used)
        chunk = stream.readline(budget)
        if not chunk:
            break
        used += len(chunk)
        line_no += 1
        # A full line at EOF need not have a trailing newline.
        over = len(chunk) > max_line_bytes
        if not chunk.endswith(b"\n") and len(chunk) == budget:
            if used >= max_bytes:
                # Read one byte only to tell exact EOF from an actual truncation.
                if stream.read(1):
                    stats["input_limit_reached"] = True
                    break
            else:
                over = True
                while used < max_bytes:
                    tail = stream.readline(min(65536, max_bytes - used))
                    if not tail:
                        break
                    used += len(tail)
                    if tail.endswith(b"\n"):
                        break
                if used >= max_bytes and not tail.endswith(b"\n"):
                    stats["input_limit_reached"] = True
        if over:
            stats["oversize_lines"] += 1
        else:
            yield line_no, chunk
        if stats["input_limit_reached"]:
            break
    if used >= max_bytes and not stats["input_limit_reached"]:
        if stream.read(1):
            stats["input_limit_reached"] = True
    stats["bytes_read_uncompressed"] = used
    stats["lines_seen"] = line_no


def default_roots() -> dict[str, list[Path]]:
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    pi = Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi" / "agent")
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    workbuddy = home / ".workbuddy"
    wb = [workbuddy / "projects"]
    # Account-scoped projects are discovered only inside this client's own home.
    if workbuddy.is_dir():
        wb.extend(p / "projects" for p in workbuddy.glob("user-*") if p.is_dir())
    return {"codex": [codex / "sessions", codex / "archived_sessions"],
            "workbuddy": wb, "pi": [pi / "sessions"], "claude": [claude / "projects"]}


def _path_subagent(path: Path):
    return any(p.lower() in {"subagents", "subagent"} for p in path.parts) or bool(
        re.match(r"agent-[\w-]+\.jsonl(?:\.zst)?$", path.name, re.I))


def _metadata(record: dict, client: str):
    kind = record.get("type")
    out = {}
    p = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if client == "codex" and kind == "session_meta":
        out.update(session_id=p.get("id") or p.get("session_id"), cwd=p.get("cwd"),
                   started_at=_iso_timestamp(p.get("timestamp") or record.get("timestamp")))
        source = p.get("source")
        thread_source = p.get("thread_source")
        out["subagent"] = bool(p.get("parent_thread_id") or
                               isinstance(source, dict) and "subagent" in source or
                               isinstance(source, str) and "subagent" in source.lower() or
                               isinstance(thread_source, dict) and "subagent" in thread_source)
    elif client == "pi" and kind == "session":
        out.update(session_id=record.get("id"), cwd=record.get("cwd"),
                   started_at=_iso_timestamp(record.get("timestamp")))
        # A parentSession is a user fork, not necessarily a background subagent.
        if record.get("parentSession"):
            out["parent_session"] = record["parentSession"]
    elif client in {"workbuddy", "claude"}:
        out.update(session_id=record.get("sessionId"), cwd=record.get("cwd"))
        if record.get("timestamp"):
            out["started_at"] = _iso_timestamp(record["timestamp"])
        if record.get("isSidechain") or record.get("is_sidechain"):
            out["subagent"] = True
        if client == "workbuddy" and kind == "session-meta":
            meta = record.get("meta")
            if isinstance(meta, dict):
                out["cwd"] = out.get("cwd") or meta.get("cwd")
    return {k: v for k, v in out.items() if v is not None}


def _probe(path, client):
    result = {}
    stats = Counter()
    try:
        with _open_source(path) as stream:
            for _, line in _bounded_lines(stream, 4 * 1024 * 1024, 1024 * 1024, stats):
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                meta = _metadata(record, client)
                for key, value in meta.items():
                    if key == "subagent":
                        result[key] = result.get(key, False) or value
                    elif key not in result:
                        result[key] = value
                if result.get("session_id") and result.get("cwd"):
                    break
    except Exception as exc:
        result["probe_error"] = str(exc) if str(exc) == "zstandard_dependency_missing" else type(exc).__name__
    return result


def discover_sessions(roots: Mapping[str, object] | None = None, *,
                      include_subagents: bool = False) -> Iterator[dict]:
    """Discover transcript files, never history.jsonl, cache, prompts, or API keys.

    ``roots`` maps client names to one directory or a sequence of directories and
    is useful for fixtures/custom installations. Missing roots produce no items.
    Read failures produce an explicit descriptor with status=unreadable.
    """
    seen = set()
    seen_sessions = set()
    for client, entries in (default_roots() if roots is None else roots).items():
        if client not in CLIENTS:
            raise ValueError(f"Unsupported client: {client}")
        if isinstance(entries, (str, Path)):
            entries = [entries]
        for entry in entries:
            root = Path(entry)
            if not root.is_dir():
                continue
            # rglob('*') would traverse assets/cache unnecessarily.
            paths = list(root.rglob("*.jsonl"))
            if client == "codex":
                paths.extend(root.rglob("*.jsonl.zst"))
            for path in sorted(paths):
                identity = os.path.normcase(str(path.resolve()))
                if identity in seen:
                    continue
                seen.add(identity)
                descriptor = {"client": client, "path": str(path), "root": str(root),
                              "session_id": path.name.removesuffix(".zst").removesuffix(".jsonl"),
                              "cwd": None, "project_hint": None, "compressed": path.suffix == ".zst",
                              "subagent": _path_subagent(path), "status": "ok",
                              "size": None, "mtime_ns": None, "fingerprint": None,
                              "fingerprint_kind": "sha256-path-size-mtime_ns"}
                try:
                    stat, fingerprint = _stat_fingerprint(path)
                    descriptor.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                      fingerprint=fingerprint)
                    probe = _probe(path, client)
                    subagent = descriptor["subagent"] or probe.pop("subagent", False)
                    descriptor.update(probe, subagent=subagent)
                    descriptor["project_hint"] = descriptor.get("cwd") or str(path.parent.relative_to(root))
                    if descriptor.get("probe_error"):
                        descriptor["status"] = "probe_error"
                except OSError as exc:
                    descriptor.update(status="unreadable", probe_error=type(exc).__name__)
                if descriptor["subagent"] and not include_subagents:
                    continue
                session_identity = (client, descriptor["session_id"])
                if session_identity in seen_sessions:
                    continue
                seen_sessions.add(session_identity)
                yield descriptor


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}:
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return ""


def _strip_embedded_tools(text):
    """Strip runtime wrappers, not arbitrary HTML or prose supplied by the user."""
    for tag in ("system-reminder", "previous_tool_call", "analysis", "thinking"):
        text = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", "", text, flags=re.I)
    return text.strip()


def _user_query_text(text):
    text = _strip_embedded_tools(text)
    queries = re.findall(r"<user_query\b[^>]*>([\s\S]*?)</user_query>", text, flags=re.I)
    return "\n".join(queries).strip() if queries else text


def _workbuddy_content(text, role):
    """Separate actual dialogue from replayed context embedded in user records.

    Old WorkBuddy versions serialize previous tool calls, identity prompts, and
    whole conversation replays inside a role=user message. Keeping that entire
    string as user speech both pollutes memories and reverses provenance.
    """
    clean = _strip_embedded_tools(text)
    remaining = clean
    while remaining.lstrip().startswith(("<cb_summary>", "<conversation_history_summary>")):
        remaining = remaining.lstrip()
        tag = "cb_summary" if remaining.startswith("<cb_summary>") else "conversation_history_summary"
        closing = f"</{tag}>"
        end = remaining.rfind(closing)
        body = remaining[len(tag) + 2:end] if end >= 0 else remaining[len(tag) + 2:]
        remaining = remaining[end + len(closing):] if end >= 0 else ""
        body = _strip_embedded_tools(body)
        if tag == "conversation_history_summary":
            if body.strip():
                yield "assistant", body.strip(), "summary", "embedded_history_summary"
            continue
        # Replay fragments retain original roles, but are explicitly marked as
        # historical summary evidence and can be deduplicated against originals.
        for match in re.finditer(r"<previous_(user|assistant)_message>([\s\S]*?)</previous_\1_message>", body, re.I):
            previous_role = match.group(1).lower()
            previous_text = _user_query_text(match.group(2)) if previous_role == "user" else _strip_embedded_tools(match.group(2))
            if previous_text:
                yield previous_role, previous_text, "summary", "embedded_previous_" + previous_role
        body = re.sub(r"<previous_(user|assistant)_message>[\s\S]*?</previous_\1_message>", "", body, flags=re.I)
        # An embedded tool block truncated by the old client cannot be useful
        # dialogue. Its arguments/results must never become a memory candidate.
        body = re.sub(r"<previous_tool_call\b[\s\S]*$", "", body, flags=re.I)
        body = re.sub(r"<omitted\s*/?>", "", body, flags=re.I).strip()
        for summary_tag in ("conversation_history_summary", "summary"):
            for value in re.findall(rf"<{summary_tag}>([\s\S]*?)</{summary_tag}>", body, re.I):
                if value.strip():
                    yield "assistant", value.strip(), "summary", "embedded_history_summary"
            body = re.sub(rf"<{summary_tag}>[\s\S]*?</{summary_tag}>", "", body, flags=re.I).strip()
        if body:
            yield "assistant", body, "summary", "embedded_history_summary"
    if remaining.strip():
        actual = _user_query_text(remaining) if role == "user" else remaining.strip()
        if actual and not actual.lstrip().startswith("<task-notification>"):
            yield role, actual, "message", "message"


def _candidates(record, client):
    """Return (role,text,kind,timestamp,origin) without ever reading tool output."""
    kind = record.get("type")
    stamp = _iso_timestamp(record.get("timestamp"))
    if client == "codex":
        p = record.get("payload")
        if not isinstance(p, dict):
            return
        if kind == "response_item" and p.get("type") == "message" and p.get("role") in {"user", "assistant"} and p.get("channel") != "analysis":
            yield p["role"], _text(p.get("content")), "message", stamp, "response_item"
        elif kind == "event_msg" and p.get("type") in {"user_message", "agent_message", "assistant_message"}:
            yield ("user" if p["type"] == "user_message" else "assistant"), _text(p.get("message")), "message", stamp, "event_msg"
        elif kind in {"compacted", "compaction"}:
            summary = p.get("message") or p.get("summary")
            if isinstance(summary, str):
                yield "assistant", summary, "summary", stamp, "compaction"
    elif client == "workbuddy":
        if kind == "message" and record.get("role") in {"user", "assistant"}:
            for role, text, item_kind, origin in _workbuddy_content(_text(record.get("content")), record["role"]):
                yield role, text, item_kind, stamp, origin
        elif kind in {"summary", "compaction", "compacted"}:
            value = record.get("summary") or record.get("content")
            if isinstance(value, str):
                yield "assistant", value, "summary", stamp, "compaction"
    elif client == "pi":
        message = record.get("message")
        if kind == "message" and isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
            yield message["role"], _text(message.get("content")), "message", _iso_timestamp(message.get("timestamp")) or stamp, "message"
        elif kind in {"compaction", "branch_summary"} and isinstance(record.get("summary"), str):
            yield "assistant", record["summary"], "summary", stamp, kind
    elif client == "claude":
        message = record.get("message")
        if kind in {"user", "assistant"} and isinstance(message, dict):
            role = message.get("role") or kind
            if role in {"user", "assistant"}:
                yield role, _text(message.get("content")), "message", stamp, "message"
        elif kind == "summary" and isinstance(record.get("summary"), str):
            yield "assistant", record["summary"], "summary", stamp, "summary"


def parse_session(descriptor: dict, *, max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
                  max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
                  max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
                  max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict:
    """Read a transcript with explicit loss indicators and tail-preserving limits.

    Keeps recent messages when max_text_chars is exceeded. A long message keeps
    its beginning and end with text_truncated=True. ``incomplete`` means caller
    must retry/review; never silently mark source as completely processed. Text
    remains untrusted and may contain quoted instructions or secrets; downstream
    extraction must enforce its own factual selection and credential redaction.
    """
    for value in (max_input_bytes, max_text_chars, max_message_chars, max_line_bytes):
        if not isinstance(value, int) or value < 1:
            raise ValueError("All limits must be positive integers")
    if descriptor.get("client") not in CLIENTS:
        raise ValueError("Unsupported client")
    desc = dict(descriptor)
    path = Path(desc["path"])
    stats = Counter()
    messages = deque()
    recent = deque(maxlen=24)
    seen_dialogue = set()
    seen_summaries = set()
    warnings = []
    text_chars = 0
    summaries = []
    start_fingerprint = None
    def warn(code):
        if code not in warnings:
            warnings.append(code)
    try:
        _, start_fingerprint = _stat_fingerprint(path)
        if desc.get("fingerprint") and start_fingerprint != desc["fingerprint"]:
            warn("source_changed_since_discovery")
        with _open_source(path) as stream:
            for line_no, line in _bounded_lines(stream, max_input_bytes, max_line_bytes, stats):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    stats["malformed_lines"] += 1
                    continue
                if not isinstance(record, dict):
                    stats["non_object_lines"] += 1
                    continue
                stats["records_read"] += 1
                for key, value in _metadata(record, desc["client"]).items():
                    if key == "subagent":
                        desc[key] = desc.get(key, False) or value
                    elif key == "session_id":
                        desc[key] = value
                    elif not desc.get(key):
                        desc[key] = value
                if record.get("isSidechain") or record.get("is_sidechain"):
                    stats["sidechain_records_skipped"] += 1
                    continue
                # Actual system/developer/tool/thinking records are never candidates.
                candidates = list(_candidates(record, desc["client"]))
                if not candidates:
                    stats["non_dialogue_records"] += 1
                for role, text, kind, stamp, origin in candidates:
                    if desc["client"] == "codex" and role == "user":
                        before = text
                        for tag in ("codex_internal_context", "turn_aborted", "task-notification", "subagent_notification", "appshot"):
                            text = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", "", text, flags=re.I).strip()
                        if text != before:
                            stats["injected_context_blocks_removed"] += 1
                    if not text.strip():
                        continue
                    if desc["client"] == "codex" and role == "user" and text.lstrip().startswith((
                            "# AGENTS.md instructions", "<environment_context>",
                            "<permissions instructions>", "<skills_instructions>",
                            "<external_codex_apps_open_page>", "<recommended_plugins>")):
                        stats["injected_context_messages_skipped"] += 1
                        continue
                    signature = hashlib.sha256((role + "\0" + text).encode("utf-8")).hexdigest()
                    if origin.startswith("embedded_previous_") and signature in seen_dialogue:
                        stats["duplicate_embedded_history_messages"] += 1
                        continue
                    if kind == "summary" and signature in seen_summaries:
                        stats["duplicate_summaries"] += 1
                        continue
                    if desc["client"] == "codex" and kind == "message" and any(
                            s == signature and o != origin and {o, origin} == {"event_msg", "response_item"}
                            and line_no - n <= 24 for s, o, n in recent):
                        stats["duplicate_mirror_events"] += 1
                        continue
                    recent.append((signature, origin, line_no))
                    seen_dialogue.add(signature)
                    if kind == "summary":
                        seen_summaries.add(signature)
                    stats["messages_seen"] += 1
                    original_chars = len(text)
                    limit = min(max_message_chars, max_text_chars)
                    shortened = original_chars > limit
                    retained_original_chars = original_chars
                    if shortened:
                        marker = "\n[... middle omitted by parser length limit ...]\n"
                        if limit <= len(marker) + 2:
                            text = text[:limit]
                            retained_original_chars = limit
                        else:
                            available = limit - len(marker)
                            head = available // 2
                            text = text[:head] + marker + text[-(available - head):]
                            retained_original_chars = available
                        stats["messages_shortened"] += 1
                        stats["text_chars_omitted"] += original_chars - retained_original_chars
                    item = {"role": role, "text": text, "timestamp": stamp, "kind": kind,
                            "source_kind": origin, "source_line": line_no,
                            "source_path": str(path), "client": desc["client"],
                            "session_id": desc.get("session_id"), "cwd": desc.get("cwd"),
                            "source_fingerprint": start_fingerprint,
                            "original_chars": original_chars,
                            "retained_original_chars": retained_original_chars, "text_truncated": shortened}
                    messages.append(item)
                    text_chars += len(text)
                    while text_chars > max_text_chars and len(messages) > 1:
                        old = messages.popleft()
                        text_chars -= len(old["text"])
                        stats["messages_dropped_for_budget"] += 1
                        stats["text_chars_omitted"] += old["retained_original_chars"]
        _, end_fingerprint = _stat_fingerprint(path)
        if start_fingerprint != end_fingerprint:
            warn("source_changed_during_read")
    except Exception as exc:
        warn(str(exc) if str(exc) == "zstandard_dependency_missing" else "read_error:" + type(exc).__name__)
    for field in ("input_limit_reached", "oversize_lines", "malformed_lines", "non_object_lines",
                  "messages_shortened", "messages_dropped_for_budget"):
        if stats[field]:
            warn(field)
    desc["project_hint"] = desc.get("cwd") or desc.get("project_hint")
    result_messages = list(messages)
    for message in result_messages:
        message["session_id"] = desc.get("session_id")
        message["cwd"] = message["cwd"] or desc.get("cwd")
        message["project_hint"] = desc.get("project_hint")
    summaries = [m for m in result_messages if m["kind"] == "summary"]
    stats["messages_retained"] = len(result_messages)
    stats["text_chars_retained"] = text_chars
    stats["summaries_retained"] = len(summaries)
    return {"descriptor": desc, "client": desc["client"], "session_id": desc.get("session_id"),
            "cwd": desc.get("cwd"), "project_hint": desc.get("project_hint"),
            "source_fingerprint": start_fingerprint, "messages": result_messages,
            "summaries": summaries, "summary": summaries[-1]["text"] if summaries else None,
            "stats": dict(stats), "warnings": warnings,
            "truncated": any(stats[k] for k in ("input_limit_reached", "oversize_lines",
                                                "messages_shortened", "messages_dropped_for_budget")),
            "incomplete": bool(warnings), "subagent": bool(desc.get("subagent"))}
