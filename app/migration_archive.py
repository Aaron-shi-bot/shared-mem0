"""Bounded memory-only snapshots and legacy authenticated migration archives.

New portable ZIP snapshots need no key or password. Their SHA-256 manifest
detects corruption; it is not a signature or confidentiality protection.
Legacy transport passwords still protect encrypted backups and recovery keys.
Historical content is data, never code. Native links belong to deployment.
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import struct
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"MEM0-MIGRATE\x00"
FORMAT = "mem0-local-transport-v1"
SNAPSHOT_FORMAT = "mem0-memory-snapshot-v1"
SNAPSHOT_MANIFEST = "snapshot-manifest.json"
MAX_TRANSPORT_BYTES = 256 * 1024 * 1024
MAX_ZIP_BYTES = 160 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_RECORDS = 100000
MAX_HEADER_BYTES = 4096
KDF = {"name": "scrypt", "n": 32768, "r": 8, "p": 1}
PROJECT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
AUTO_RE = re.compile(r"^[a-f0-9]{64}$")
AGENTS = {"codex", "workbuddy", "pi", "claude", "manual", "import"}
NATIVE_SUFFIXES = {".md", ".txt", ".json", ".db", ".sqlite", ".sqlite3"}
SKIP_ASSETS = {"automation/state.sqlite", "automation/config.json", "native/mirror-manifest.json"}
SNAPSHOT_TEXT_SUFFIXES = {".md", ".txt"}
SNAPSHOT_EXCLUSION_REASONS = {"runtime_state", "database", "deployment", "derived_manifest", "not_memory_asset"}
_SECRET_KEYS = re.compile(r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|client[_-]?secret|private[_-]?key|recovery[_-]?key|local[_-]?api[_-]?key)$", re.I)
_SECRET_VALUES = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{12,})\b|"
    r"\b(?:lsv2_pt_|ls_|lf_|(?:sk|pk)[_-]lf[_-])[A-Za-z0-9_-]{16,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:vless|vmess|trojan|ss|ssr)://[^\s<>\"']{12,}|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*|"
    r"https?://[^\s/:@]+:[^\s/@]+@", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(?<![\w-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|passphrase|client[_-]?secret|recovery[_-]?key|local[_-]?api[_-]?key|密码|口令|密钥|令牌)"
    r"[\"']?\s*[:=：]\s*[\"'`]?([A-Za-z0-9_./+=$:{}@!%?&~*-]{12,})")
_SECRET_TABLE = re.compile(r"(?im)\|\s*[*_`]*(?:password|passwd|passphrase|secret|api[_ -]?key|access[_ -]?token|token|密码|口令|密钥|令牌)[*_`]*\s*\|\s*([^|\r\n]+)")
_SECRET_QUERY = re.compile(r"[?&](?:token|access_token|key|secret|password)=([^\s&#]{16,})", re.I)
DEVICE_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)


class MigrationError(Exception):
    """Safe machine-readable error. Never contains passwords, keys or API data."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code, message):
    raise MigrationError(code, message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("invalid_json", "备份 JSON 含重复字段。")
            result[key] = value
        return result
    try:
        return json.loads(body, object_pairs_hook=unique,
                          parse_constant=lambda _: _fail("invalid_json", "备份 JSON 含无效数值。"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_json", "备份 JSON 格式无效。")


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_bounded(path, limit):
    try:
        with Path(path).open("rb") as stream:
            body = stream.read(limit + 1)
    except OSError:
        _fail("source_read_failed", "无法读取迁移源文件，请检查文件是否存在及权限。")
    if len(body) > limit:
        _fail("size_limit", "迁移源文件超过大小上限，未写入目标。")
    return body


def _password_key(password, salt):
    if not isinstance(password, str) or len(password) < 10:
        _fail("weak_password", "迁移口令至少需要 10 个字符。")
    encoded = password.encode("utf-8")
    if len(encoded) > 1024:
        _fail("invalid_password", "迁移口令过长。")
    return Scrypt(salt=salt, length=32, n=KDF["n"], r=KDF["r"], p=KDF["p"]).derive(encoded)


def _reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _root(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        _fail("absolute_path_required", "目标必须是绝对路径。")
    absolute = Path(os.path.abspath(path))
    if absolute.parent == absolute or _reparse(absolute) or absolute.resolve() != absolute:
        _fail("unsafe_destination", "目标不能是磁盘根目录或重解析链接。")
    return absolute


def _safe_child(root, relative):
    root = _root(root)
    parts = PurePosixPath(relative).parts
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        _fail("unsafe_destination", "目标路径越过指定目录边界。")
    for candidate in (root, *[root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]):
        if _reparse(candidate):
            _fail("unsafe_destination", "目标含符号链接或 junction，未写入该路径。")
    return target


def _write_new(path, body):
    """No overwrite, including a file created after preflight."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _fail("destination_exists", "目标已经存在，未覆盖已有文件。")
    except OSError:
        if created:
            # Exact file we created, never a computed recursive deletion.
            path.unlink(missing_ok=True)
        _fail("target_write_failed", "无法写入目标文件，已有文件保持不变。")


def _member_name(info):
    raw = info.filename
    if not raw or "\x00" in info.orig_filename or "\\" in info.orig_filename or raw.startswith("/") or ":" in raw:
        _fail("unsafe_zip_path", "备份含不安全的 ZIP 路径。")
    trimmed = raw[:-1] if raw.endswith("/") else raw
    parts = trimmed.split("/")
    if any(not p or p in (".", "..") or p.endswith((" ", ".")) or
           any(ord(ch) < 32 or ch in '<>"|?*' for ch in p) or DEVICE_RE.fullmatch(p)
           for p in parts):
        _fail("unsafe_zip_path", "备份含不安全的 ZIP 路径。")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
        _fail("unsafe_zip_type", "备份不允许符号链接或特殊设备文件。")
    if info.flag_bits & 1:
        _fail("unsupported_zip", "不支持内部另加密码的 ZIP。")
    allowed = (
        trimmed in {"memories.json", "README.txt", "memory-assets-manifest.json", SNAPSHOT_MANIFEST} or
        parts[0] == "native" or
        trimmed in {"automation", "automation/projects.json", "automation/config.json", "automation/state.sqlite"} or
        trimmed in {"deployment", "deployment/app", "deployment/scripts",
                    "deployment/requirements.lock.txt", "deployment/settings.template.json"} or
        (len(parts) == 3 and parts[:2] in (["deployment", "app"], ["deployment", "scripts"])
         and PurePosixPath(trimmed).suffix.lower() in {".py", ".ps1", ".ts", ".html"})
    )
    if not allowed:
        _fail("unsupported_asset", "备份含当前迁移格式不支持的文件。")
    if parts[0] == "native" and not info.is_dir():
        if PurePosixPath(trimmed).suffix.lower() not in NATIVE_SUFFIXES:
            _fail("unsupported_asset", "原生记忆资产格式不受支持。")
    if parts[-1].lower() in {"recovery.key", "api.token", "auth.json", ".credentials.yaml"}:
        _fail("credential_asset", "备份不能包含恢复密钥或客户端凭据文件。")
    return trimmed


def _record_plan(doc):
    if not isinstance(doc, dict) or doc.get("format") != "mem0-local-portable-v1":
        _fail("unsupported_backup", "不是支持的 Mem0 可迁移备份格式。")
    records = doc.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        _fail("invalid_records", "记忆记录列表格式或数量不符合要求。")
    plans = []
    automatic_seen = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("memory"), str):
            _fail("invalid_record", "备份含无效记忆记录。")
        text = record["memory"].strip()
        meta = record.get("metadata", {})
        if not text or len(text) > 16000 or not isinstance(meta, dict):
            _fail("invalid_record", "记忆正文或元数据不符合服务恢复要求。")
        scope = meta.get("scope", "project")
        agent = meta.get("source_agent", "import")
        project = "global" if scope == "global" else meta.get("project", "restored")
        task = meta.get("task_id", "")
        source = meta.get("source", "restored-backup")
        if scope not in {"global", "project", "task"} or agent not in AGENTS:
            _fail("invalid_record", "记忆范围或来源客户端无效。")
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            _fail("invalid_record", "备份含无效的稳定项目 ID。")
        if not isinstance(task, str) or not isinstance(source, str) or (scope == "task" and not task):
            _fail("invalid_record", "任务 ID 或来源字段无效。")
        payload = {"text": text, "scope": scope, "project": project, "task_id": task,
                   "source_agent": agent, "source": source, "infer": False}
        if meta.get("automatic") and not meta.get("auto_key"):
            _fail("invalid_auto_record", "自动摘要缺少稳定标识，不能按普通记忆恢复。")
        if meta.get("automatic") and meta.get("auto_key"):
            key = meta["auto_key"]
            part = meta.get("part")
            session_id = meta.get("session_id")
            if (not isinstance(key, str) or not AUTO_RE.fullmatch(key) or agent not in AGENTS - {"manual", "import"}
                    or not isinstance(part, int) or isinstance(part, bool) or part < 1
                    or not isinstance(session_id, str) or not session_id or scope != "project"):
                _fail("invalid_auto_record", "自动摘要的稳定标识或来源信息不完整。")
            payload = {"auto_key": key, "text": text, "source_agent": agent, "project": project,
                       "task_id": task, "session_id": session_id, "part": part, "source": source}
            if key in automatic_seen and automatic_seen[key] != payload:
                _fail("conflicting_auto_records", "备份内同一自动摘要标识对应不同内容。")
            automatic_seen[key] = payload
            route = "/api/automation/restore-record"
        else:
            route = "/api/memories"
        plans.append({"index": index, "route": route, "payload": payload})
    return plans


def _validate_zip(zipbytes):
    if not isinstance(zipbytes, bytes) or len(zipbytes) > MAX_ZIP_BYTES:
        _fail("size_limit", "解密备份超过大小上限。")
    entries = {}
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zipbytes)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                _fail("size_limit", "备份文件数量超过上限。")
            for info in infos:
                name = _member_name(info)
                identity = name.casefold()
                if identity in seen:
                    _fail("duplicate_zip_path", "备份含重复或大小写冲突路径。")
                seen.add(identity)
                if info.file_size > MAX_MEMBER_BYTES:
                    _fail("size_limit", "备份内单文件超过大小上限。")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    _fail("size_limit", "备份解压总大小超过上限。")
                entries[name] = info
            casefold_entries = {name.casefold(): info for name, info in entries.items()}
            for name, info in entries.items():
                for parent in PurePosixPath(name).parents:
                    other = casefold_entries.get(str(parent).casefold())
                    if other and not other.is_dir():
                        _fail("unsafe_zip_path", "ZIP 内文件和目录路径冲突。")
            # Fully check CRC and lengths before any caller can write a target.
            bodies = {}
            for name, info in entries.items():
                if info.is_dir():
                    continue
                with archive.open(info) as stream:
                    body = stream.read(MAX_MEMBER_BYTES + 1)
                if len(body) != info.file_size:
                    _fail("invalid_zip", "ZIP 文件大小校验失败。")
                bodies[name] = body
    except MigrationError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError):
        _fail("invalid_zip", "ZIP 损坏或使用不支持的压缩格式。")
    if "memories.json" not in bodies:
        _fail("invalid_backup", "备份缺少 memories.json。")
    doc = _json(bodies["memories.json"])
    plans = _record_plan(doc)
    if "automation/projects.json" in bodies:
        _project_registry(_json(bodies["automation/projects.json"]))
    manifest = _json(bodies["memory-assets-manifest.json"]) if "memory-assets-manifest.json" in bodies else {}
    if not isinstance(manifest, dict):
        _fail("invalid_backup", "资产清单格式无效。")
    if SNAPSHOT_MANIFEST in bodies:
        _validate_snapshot(bodies, doc, plans)
    return bodies, doc, plans, manifest


def _placeholder(value):
    if not isinstance(value, str):
        return value is None
    value = value.strip()
    return (not value or value.lower() in {"redacted", "[redacted]", "<redacted>", "none", "null",
                                          "placeholder", "your-api-key", "your_api_key", "changeme"}
            or bool(re.fullmatch(r"[xX*•●]+", value))
            or value.startswith(("${", "$env:", "os.environ[", "process.env.")))


def _check_snapshot_content(value):
    """Reject actual credential structures/formats, not prose naming a key.

    This is a conservative preflight, not a claim to identify every possible
    secret in arbitrary prose. The caller supplies already curated memories.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEYS.fullmatch(str(key)) and not _placeholder(item):
                _fail("credential_content", "记忆资产含疑似凭据值，请先移除后再打包。")
            if str(key).lower() in {"messages", "raw_chat", "raw_session", "transcript", "tool_calls"} and isinstance(item, (list, dict)) and item:
                _fail("raw_session_content", "便携快照仅接受提炼后的记忆，不能包含原始会话结构。")
            _check_snapshot_content(item)
    elif isinstance(value, list):
        for item in value:
            _check_snapshot_content(item)
    elif isinstance(value, str):
        table_values = [m.group(1).strip(' `*_') for m in _SECRET_TABLE.finditer(value)]
        if (_SECRET_VALUES.search(value)
                or any(not _placeholder(m.group(1)) for m in _SECRET_ASSIGNMENT.finditer(value))
                or any(not _placeholder(m.group(1)) for m in _SECRET_QUERY.finditer(value))
                or any(re.fullmatch(r'[A-Za-z0-9_./+=$:{}@!%?&~*-]{8,}', item) and not _placeholder(item) for item in table_values)):
            _fail("credential_content", "记忆资产含疑似凭据值，请先移除后再打包。")


def _snapshot_asset_reason(name):
    """None means an explicitly allowed memory asset; strings explain omission."""
    path = PurePosixPath(name)
    parts = path.parts
    if name == "automation/projects.json":
        return None
    if name in {"automation/config.json", "automation/state.sqlite"}:
        return "runtime_state"
    if name in {"native/mirror-manifest.json", "memory-assets-manifest.json", SNAPSHOT_MANIFEST, "README.txt"}:
        return "derived_manifest"
    if parts and parts[0] == "deployment":
        return "deployment"
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return "database"
    if path.suffix.lower() not in SNAPSHOT_TEXT_SUFFIXES:
        return "not_memory_asset"
    if any(p.lower() in {"sessions", "archived_sessions", "transcripts", "raw", ".git", "__pycache__"} for p in parts):
        return "not_memory_asset"
    prefixes = ("native/codex/memories/", "native/workbuddy/memory/",
                "native/pi/pi-hermes-memory/", "native/pi/projects-memory/")
    if name.startswith(prefixes):
        return None
    if name == "native/workbuddy/legacy/MEMORY.md":
        return None
    if len(parts) == 5 and parts[:3] == ("native", "workbuddy", "accounts") and parts[3].startswith("user-") and parts[4] == "MEMORY.md":
        return None
    if len(parts) >= 6 and parts[:3] == ("native", "claude", "projects") and parts[4] == "memory":
        return None
    return "not_memory_asset"


def _snapshot_assets(assets):
    if not isinstance(assets, dict):
        _fail("invalid_snapshot", "记忆资产必须是相对路径到字节内容的映射。")
    included, excluded, identities = {}, [], set()
    if len(assets) + 2 > MAX_MEMBERS:
        _fail("size_limit", "快照文件数量超过上限。")
    total = 0
    for name, body in assets.items():
        if not isinstance(name, str) or not isinstance(body, bytes):
            _fail("invalid_snapshot", "记忆资产必须使用字符串路径和 bytes 内容。")
        info = zipfile.ZipInfo(name)
        # Validate before omission, so bad paths are never silently accepted.
        name = _member_name(info)
        if name.casefold() in identities:
            _fail("duplicate_zip_path", "快照含重复或大小写冲突路径。")
        identities.add(name.casefold())
        if name == "memories.json" or info.is_dir():
            _fail("invalid_snapshot", "资产不能替换共享记忆文档或包含目录条目。")
        if len(body) > MAX_MEMBER_BYTES:
            _fail("size_limit", "快照单文件超过大小上限。")
        total += len(body)
        if total > MAX_UNCOMPRESSED_BYTES:
            _fail("size_limit", "快照资产总大小超过上限。")
        reason = _snapshot_asset_reason(name)
        if reason:
            excluded.append({"path": name, "reason": reason})
            continue
        try:
            text = body.decode("utf-8-sig")
        except UnicodeError:
            _fail("invalid_memory_text", "便携原生记忆必须是 UTF-8 文本。")
        if name == "automation/projects.json":
            _check_snapshot_content(_project_registry(_json(body)))
        else:
            _check_snapshot_content(text)
        included[name] = body
    return included, sorted(excluded, key=lambda item: item["path"])


def _validate_snapshot(bodies, doc, plans):
    manifest = _json(bodies[SNAPSHOT_MANIFEST])
    fields = {"format", "version", "created_at", "backup_created_at", "records", "encrypted",
              "content_type", "hash_algorithm", "files", "excluded", "asset_warning_count"}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest.get("format") != SNAPSHOT_FORMAT or type(manifest.get("version")) is not int or manifest["version"] != 1
            or manifest.get("encrypted") is not False or manifest.get("content_type") != "memory-only"
            or manifest.get("hash_algorithm") != "sha256" or type(manifest.get("records")) is not int
            or manifest["records"] != len(plans) or not isinstance(manifest.get("created_at"), str)
            or not isinstance(manifest.get("backup_created_at"), (str, type(None)))
            or type(manifest.get("asset_warning_count")) is not int or manifest["asset_warning_count"] < 0):
        _fail("invalid_snapshot", "便携快照格式、版本或记录计数无效。")
    files = manifest.get("files")
    payload_names = set(bodies) - {SNAPSHOT_MANIFEST}
    if not isinstance(files, dict) or set(files) != payload_names:
        _fail("snapshot_integrity_failed", "快照文件列表与完整性清单不一致。")
    for name in payload_names:
        entry = files[name]
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or type(entry.get("size")) is not int or entry["size"] != len(bodies[name])
                or entry.get("sha256") != hashlib.sha256(bodies[name]).hexdigest()):
            _fail("snapshot_integrity_failed", "快照文件大小或 SHA-256 校验失败。")
        if name != "memories.json" and _snapshot_asset_reason(name) is not None:
            _fail("unsupported_snapshot_asset", "便携快照含运行状态、数据库或非记忆文件。")
    excluded = manifest.get("excluded")
    if not isinstance(excluded, list) or len(excluded) > MAX_MEMBERS:
        _fail("invalid_snapshot", "快照排除清单无效。")
    for item in excluded:
        if (not isinstance(item, dict) or set(item) != {"path", "reason"}
                or not isinstance(item.get("path"), str) or item.get("reason") not in SNAPSHOT_EXCLUSION_REASONS):
            _fail("invalid_snapshot", "快照排除清单无效。")
        _member_name(zipfile.ZipInfo(item["path"]))
    if set(doc) != {"format", "created_at", "records"}:
        _fail("invalid_snapshot", "快照记忆文档含不支持的顶层字段。")
    _check_snapshot_content(doc)
    _snapshot_assets({name: bodies[name] for name in payload_names if name != "memories.json"})
    return manifest


def _build_snapshot(records_doc, assets=None, asset_warning_count=0):
    plans = _record_plan(records_doc)
    doc = {"format": "mem0-local-portable-v1", "created_at": records_doc.get("created_at") or _now(),
           "records": records_doc["records"]}
    if not isinstance(doc["created_at"], str):
        _fail("invalid_snapshot", "快照记录时间必须是字符串。")
    _check_snapshot_content(doc)
    bodies, excluded = _snapshot_assets({} if assets is None else assets)
    bodies["memories.json"] = _serialize(doc)
    manifest = {"format": SNAPSHOT_FORMAT, "version": 1, "created_at": _now(),
                "backup_created_at": doc["created_at"], "records": len(plans), "encrypted": False,
                "content_type": "memory-only", "hash_algorithm": "sha256",
                "files": {name: {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
                          for name, body in sorted(bodies.items())},
                "excluded": excluded, "asset_warning_count": asset_warning_count}
    bodies[SNAPSHOT_MANIFEST] = _serialize(manifest)
    if any(len(body) > MAX_MEMBER_BYTES for body in bodies.values()) or sum(map(len, bodies.values())) > MAX_UNCOMPRESSED_BYTES:
        _fail("size_limit", "快照内容超过大小上限。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(bodies.items()):
            archive.writestr(name, body)
    zipbytes = buffer.getvalue()
    _validate_zip(zipbytes)
    return zipbytes


def build_snapshot(records_doc: dict, assets: dict[str, bytes] | None = None) -> bytes:
    """Build a password-free ZIP from curated records and memory text assets.

    records_doc uses the existing mem0-local-portable-v1 shape. Record IDs and
    metadata are retained verbatim; existing restore APIs retain stable project
    IDs/auto_key/source and deduplicate content. Disallowed legacy assets are
    explicitly listed as excluded. Credentials fail before writing anything.
    """
    return _build_snapshot(records_doc, assets)


def create_snapshot(zipbytes: bytes, destination: Path) -> dict:
    """Validate/convert a portable ZIP and write a new file without a password.

    Accepts either a new snapshot or the plaintext ZIP from a legacy transport.
    Never reads an encryption key, live database or raw session from disk.
    """
    bodies, doc, _, legacy_manifest = _validate_zip(zipbytes)
    if SNAPSHOT_MANIFEST in bodies:
        snapshot = zipbytes
    else:
        warnings = legacy_manifest.get("errors", [])
        snapshot = _build_snapshot(doc, {name: body for name, body in bodies.items() if name != "memories.json"},
                                   len(warnings) if isinstance(warnings, list) else 1)
    validated, _, _, _ = _validate_zip(snapshot)
    manifest = _json(validated[SNAPSHOT_MANIFEST])
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        _fail("absolute_path_required", "快照目标必须是绝对路径。")
    destination = _safe_child(_root(destination.parent), destination.name)
    _write_new(destination, snapshot)
    return {**manifest, "path": str(destination), "size": len(snapshot), "sha256": hashlib.sha256(snapshot).hexdigest()}


def open_snapshot(path: Path) -> tuple[bytes, dict]:
    """Fully validate a password-free snapshot before any restore mutation."""
    body = _read_bounded(path, MAX_ZIP_BYTES)
    bodies, _, _, _ = _validate_zip(body)
    if SNAPSHOT_MANIFEST not in bodies:
        _fail("unsupported_snapshot", "此 ZIP 不是带完整性清单的便携记忆快照。")
    return body, _json(bodies[SNAPSHOT_MANIFEST])


def create_transport(backup_path: Path, recovery_key_path: Path, password: str,
                     destination: Path) -> dict:
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        _fail("absolute_path_required", "迁移包目标必须是绝对路径。")
    parent = _root(destination.parent)
    destination = _safe_child(parent, destination.name)
    if destination.exists():
        _fail("destination_exists", "迁移包已存在，请选择新文件名。")
    if parent == Path(recovery_key_path).resolve().parent:
        _fail("key_colocation", "迁移包不能保存到恢复密钥所在目录。")
    encrypted_backup = _read_bounded(backup_path, MAX_TRANSPORT_BYTES - 8192)
    recovery_key = _read_bounded(recovery_key_path, 1024).strip()
    try:
        plaintext = Fernet(recovery_key).decrypt(encrypted_backup)
    except (ValueError, InvalidToken):
        _fail("backup_decrypt_failed", "原备份与恢复密钥不匹配，或备份已损坏。")
    _, doc, plans, manifest = _validate_zip(plaintext)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = {"format": FORMAT, "version": 1, "kdf": KDF, "cipher": "AES-256-GCM",
              "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
              "created_at": _now(), "backup_created_at": doc.get("created_at"),
              "records": len(plans), "backup_sha256": hashlib.sha256(encrypted_backup).hexdigest(),
              "asset_warning_count": len(manifest.get("errors", []))}
    header_bytes = _serialize(header)
    aad = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes
    payload = struct.pack(">H", len(recovery_key)) + recovery_key + encrypted_backup
    ciphertext = AESGCM(_password_key(password, salt)).encrypt(nonce, payload, aad)
    package = aad + ciphertext
    if len(package) > MAX_TRANSPORT_BYTES:
        _fail("size_limit", "迁移包超过大小上限。")
    _write_new(destination, package)
    return {**header, "path": str(destination), "size": len(package)}


def open_transport(transport_path: Path, password: str) -> tuple[bytes, bytes, dict]:
    body = _read_bounded(transport_path, MAX_TRANSPORT_BYTES)
    prefix_len = len(MAGIC) + 4
    if len(body) < prefix_len + 16 or not body.startswith(MAGIC):
        _fail("unsupported_transport", "不是支持的 Mem0 迁移包。")
    header_len = struct.unpack(">I", body[len(MAGIC):prefix_len])[0]
    if not 1 <= header_len <= MAX_HEADER_BYTES or prefix_len + header_len + 16 > len(body):
        _fail("invalid_transport", "迁移包头长度无效。")
    header = _json(body[prefix_len:prefix_len + header_len])
    if (not isinstance(header, dict) or header.get("format") != FORMAT or header.get("version") != 1
            or header.get("kdf") != KDF or header.get("cipher") != "AES-256-GCM"):
        _fail("unsupported_transport", "迁移包版本或加密参数不受支持。")
    try:
        salt = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        _fail("invalid_transport", "迁移包加密参数无效。")
    aad = body[:prefix_len + header_len]
    try:
        payload = AESGCM(_password_key(password, salt)).decrypt(nonce, body[len(aad):], aad)
    except InvalidTag:
        _fail("authentication_failed", "口令不正确或迁移包已被修改，未写入目标。")
    if len(payload) < 2:
        _fail("invalid_transport", "迁移包内容不完整。")
    key_len = struct.unpack(">H", payload[:2])[0]
    if key_len != 44 or len(payload) <= 2 + key_len:
        _fail("invalid_transport", "迁移包恢复材料无效。")
    recovery_key, backup = payload[2:2 + key_len], payload[2 + key_len:]
    if hashlib.sha256(backup).hexdigest() != header.get("backup_sha256"):
        _fail("invalid_transport", "迁移包内部备份校验失败。")
    try:
        plain = Fernet(recovery_key).decrypt(backup)
    except (ValueError, InvalidToken):
        _fail("backup_decrypt_failed", "迁移包内部备份无法解密。")
    _, _, plans, _ = _validate_zip(plain)
    if len(plans) != header.get("records"):
        _fail("invalid_transport", "迁移包记录数量校验失败。")
    return plain, recovery_key, header


def extract_assets(zipbytes: bytes, destination: Path) -> dict:
    bodies, _, _, _ = _validate_zip(zipbytes)
    root = _root(destination)
    if root.exists():
        _fail("destination_exists", "提取目标必须是尚不存在的新目录。")
    targets = {name: _safe_child(root, name) for name in bodies if name not in SKIP_ASSETS}
    # Do not extract raw collector state/config or execute deployment code.
    root.mkdir(parents=True, exist_ok=False)
    files = []
    try:
        for name, target in targets.items():
            _write_new(target, bodies[name])
            files.append(name)
    except MigrationError:
        # Keep a partial extraction for diagnosis; never touch other directories.
        raise
    return {"destination": str(root), "files": files,
            "skipped": sorted(set(bodies) & SKIP_ASSETS)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_base_url(base_url):
    parsed = urlsplit(base_url)
    try:
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        local = False
    if (parsed.scheme != "http" or not local or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        _fail("unsafe_api_url", "恢复 API 仅允许本机 HTTP 地址，不能附带账号或路径。")
    return base_url.rstrip("/")


def _api(base_url, api_token, method, path, payload=None):
    request = Request(base_url + path, data=_serialize(payload) if payload is not None else None,
                      method=method, headers={"Authorization": "Bearer " + api_token,
                                              "Content-Type": "application/json"})
    try:
        # Never inherit proxy settings or forward the local token through redirects.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=120) as response:
            raw = response.read(MAX_MEMBER_BYTES + 1)
        if len(raw) > MAX_MEMBER_BYTES:
            _fail("api_response_limit", "恢复服务返回的数据过大。")
        result = _json(raw)
        if not isinstance(result, dict):
            _fail("api_response_invalid", "恢复服务返回格式无效。")
        return result
    except HTTPError as exc:
        _fail("api_http_error", "本机恢复服务拒绝请求（HTTP %d），可修复后重试。" % exc.code)
    except (URLError, TimeoutError, OSError):
        _fail("api_unavailable", "无法连接本机恢复服务，可启动服务后重试。")


def _identity(payload):
    return (payload.get("scope", "project"), payload["project"],
            payload.get("task_id", "") if payload.get("scope") == "task" else "", payload["text"].strip())


def restore_records(zipbytes: bytes, base_url: str, api_token: str) -> dict:
    _, document, plans, _ = _validate_zip(zipbytes)
    base_url = _validated_base_url(base_url)
    if not isinstance(api_token, str) or not api_token or "\r" in api_token or "\n" in api_token:
        _fail("invalid_api_token", "请提供有效的本机服务访问令牌。")
    from memory_lifecycle import validate_lifecycle
    validate_lifecycle(document['records'])
    restored = 0
    while True:
        result = _api(base_url, api_token, 'POST', '/api/restore/records', {'document': document})
        batch_restored = result.get('restored', 0)
        restored += batch_restored
        if not result.get('pending'):
            result['restored'] = restored
            result['skipped'] = max(0, result.get('skipped', 0) - (restored - batch_restored))
            return result
        if not result.get('restored'):
            _fail('restore_no_progress', '恢复未取得进展，请保留快照并检查冲突。')


def _restore_records_legacy(zipbytes: bytes, base_url: str, api_token: str) -> dict:
    _, _, plans, _ = _validate_zip(zipbytes)
    existing = _api(base_url, api_token, "GET", "/api/memories").get("results")
    if not isinstance(existing, list):
        _fail("api_response_invalid", "本机服务未返回记忆列表。")
    identities = set()
    autos = {}
    for record in existing:
        if not isinstance(record, dict):
            continue
        meta = record.get("metadata", {})
        text = record.get("memory")
        if not isinstance(meta, dict) or not isinstance(text, str):
            continue
        scope = meta.get("scope", "project")
        identity = (scope, "global" if scope == "global" else meta.get("project", "restored"),
                    meta.get("task_id", "") if scope == "task" else "", text.strip())
        identities.add(identity)
        if meta.get("auto_key"):
            autos[meta["auto_key"]] = (meta.get("project"), text.strip())
    result = {"total": len(plans), "restored": 0, "skipped": 0, "updated": 0,
              "failed": 0, "errors": [], "conflicts": [], "complete": True}
    for plan in plans:
        payload = plan["payload"]
        auto_key = payload.get("auto_key")
        if auto_key:
            prior = autos.get(auto_key)
            if prior == (payload["project"], payload["text"]):
                result["skipped"] += 1
                continue
            if prior is not None:
                result["conflicts"].append({"record_index": plan["index"],
                                            "kind": "automatic_memory", "resolution": "kept_existing"})
                result["complete"] = False
                continue
        elif _identity(payload) in identities:
            result["skipped"] += 1
            continue
        try:
            _api(base_url, api_token, "POST", plan["route"], payload)
            if auto_key:
                result["updated" if auto_key in autos else "restored"] += 1
                autos[auto_key] = (payload["project"], payload["text"])
            else:
                result["restored"] += 1
                identities.add(_identity(payload))
        except MigrationError as exc:
            result["failed"] += 1
            result["errors"].append({"record_index": plan["index"], "code": exc.code})
            result["complete"] = False
            # Connection/auth failures affect remaining records. Make resumability
            # explicit, rather than claiming an all-or-nothing remote transaction.
            result["not_attempted"] = len(plans) - plan["index"] - 1
            break
    return result


def _project_registry(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("projects", {}), dict) or not isinstance(doc.get("aliases", []), list):
        _fail("invalid_projects", "项目映射格式无效。")
    for project, info in doc.get("projects", {}).items():
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project) or not isinstance(info, dict):
            _fail("invalid_projects", "项目映射含无效项目 ID。")
    for alias in doc.get("aliases", []):
        if (not isinstance(alias, dict) or not isinstance(alias.get("id"), str) or
                not PROJECT_RE.fullmatch(alias["id"]) or not isinstance(alias.get("path"), str)):
            _fail("invalid_projects", "项目路径别名格式无效。")
    return doc


def _path_identity(value):
    return str(value).replace("\\", "/").rstrip("/").casefold()


def _merge_projects(existing, incoming, mappings):
    merged = json.loads(json.dumps(existing))
    projects = merged.setdefault("projects", {})
    aliases = merged.setdefault("aliases", [])
    conflicts = []
    for key, info in incoming.get("projects", {}).items():
        if key not in projects:
            projects[key] = info
        elif projects[key] != info:
            conflicts.append({"kind": "project_metadata", "project": key, "resolution": "kept_existing"})
    known = {_path_identity(a["path"]): a["id"] for a in aliases}
    candidates = list(incoming.get("aliases", []))
    # Existing aliases and IDs always win. Explicit new-machine mappings add a
    # path alias; they do not rename stable IDs or discard original paths.
    for project, new_path in mappings.items():
        if project not in projects or not isinstance(new_path, str) or not Path(new_path).is_absolute():
            _fail("invalid_project_mapping", "新机项目映射须使用已有稳定 ID 和绝对路径。")
        candidates.append({"path": new_path, "id": project})
    for alias in candidates:
        identity = _path_identity(alias["path"])
        if identity in known and known[identity] != alias["id"]:
            conflicts.append({"kind": "project_alias", "path": alias["path"],
                              "project": alias["id"], "resolution": "kept_existing"})
        elif identity not in known:
            aliases.append(alias)
            known[identity] = alias["id"]
    return merged, conflicts


def restore_native_and_projects(extracted: Path, data_root: Path, user_home: Path,
                                project_mappings: dict | None = None) -> dict:
    """Restore native assets into D:, preserve conflicts, return junction plan.

    Caller must stop target collectors/clients before this operation. Existing
    native files are never overwritten. projects.json is a structured merge with
    a byte-for-byte before-copy and optimistic concurrency check. Old collector
    state.sqlite and config.json are never copied into the target service.
    """
    extracted, data_root, user_home = _root(extracted), _root(data_root), _root(user_home)
    if not extracted.is_dir() or data_root == extracted or data_root.is_relative_to(extracted):
        _fail("unsafe_destination", "恢复目标必须与提取目录分离。")
    mappings = project_mappings or {}
    if not isinstance(mappings, dict):
        _fail("invalid_project_mapping", "项目映射必须是稳定 ID 到绝对路径的对象。")
    # Pre-read and validate every source before any destination mutation.
    source_files = []
    total = 0
    native_source = _safe_child(extracted, "native")
    if native_source.exists():
        for path in sorted(native_source.rglob("*")):
            relative = path.relative_to(extracted).as_posix()
            checked = _safe_child(extracted, relative)
            if relative in SKIP_ASSETS:
                continue
            if checked.is_dir():
                continue
            if checked.suffix.lower() not in NATIVE_SUFFIXES:
                _fail("unsupported_asset", "提取目录含不支持的原生记忆文件。")
            if checked.name.lower() in {"auth.json", "recovery.key", "api.token", ".credentials.yaml"}:
                _fail("credential_asset", "提取目录不能包含客户端凭据文件。")
            body = _read_bounded(checked, MAX_MEMBER_BYTES)
            total += len(body)
            if total > MAX_UNCOMPRESSED_BYTES:
                _fail("size_limit", "原生记忆总大小超过上限。")
            source_files.append((relative, body, _safe_child(data_root, relative)))
    incoming_path = _safe_child(extracted, "automation/projects.json")
    incoming = _project_registry(_json(_read_bounded(incoming_path, MAX_MEMBER_BYTES))) if incoming_path.exists() else {"projects": {}, "aliases": []}
    registry_path = _safe_child(data_root, "automation/projects.json")
    old_bytes = _read_bounded(registry_path, MAX_MEMBER_BYTES) if registry_path.exists() else None
    existing = _project_registry(_json(old_bytes)) if old_bytes is not None else {"projects": {}, "aliases": []}
    merged, conflicts = _merge_projects(existing, incoming, mappings)
    result = {"copied": 0, "skipped": 0, "conflicts": conflicts, "errors": [],
              "projects": len(merged["projects"]), "link_plan": [], "file_copy_plan": [],
              "ignored": sorted(SKIP_ASSETS), "complete": True}
    run = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    report_dir = _safe_child(data_root, "backups/migration/" + run)
    report_dir.mkdir(parents=True, exist_ok=False)
    for relative, body, target in source_files:
        if target.exists():
            if target.is_file() and _read_bounded(target, MAX_MEMBER_BYTES) == body:
                result["skipped"] += 1
            else:
                result["conflicts"].append({"kind": "native_file", "path": relative, "resolution": "kept_existing"})
            continue
        try:
            _write_new(target, body)
            result["copied"] += 1
        except MigrationError as exc:
            result["errors"].append({"path": relative, "code": exc.code})
    if old_bytes is None:
        try:
            _write_new(registry_path, _serialize(merged))
        except MigrationError as exc:
            result["errors"].append({"path": "automation/projects.json", "code": exc.code})
    elif _serialize(existing) != _serialize(merged):
        _write_new(report_dir / "projects.before.json", old_bytes)
        current = _read_bounded(registry_path, MAX_MEMBER_BYTES)
        if current != old_bytes:
            result["conflicts"].append({"kind": "concurrent_project_update", "resolution": "kept_existing"})
        else:
            temporary = _safe_child(data_root, "automation/projects.migration-" + secrets.token_hex(8) + ".tmp")
            _write_new(temporary, _serialize(merged))
            # Recheck immediately before the single-file atomic replacement.
            if _read_bounded(registry_path, MAX_MEMBER_BYTES) != old_bytes:
                temporary.unlink(missing_ok=True)
                result["conflicts"].append({"kind": "concurrent_project_update", "resolution": "kept_existing"})
            else:
                os.replace(temporary, registry_path)
    links = [
        (".codex/memories", "native/codex/memories"),
        (".workbuddy/memory", "native/workbuddy/memory"),
        (".pi/agent/pi-hermes-memory", "native/pi/pi-hermes-memory"),
        (".pi/agent/projects-memory", "native/pi/projects-memory"),
    ]
    claude_root = data_root / "native/claude/projects"
    if claude_root.is_dir():
        for path in sorted(claude_root.glob("*/memory")):
            if path.is_dir():
                links.append((".claude/projects/" + path.parent.name + "/memory",
                              path.relative_to(data_root).as_posix()))
    for relative, target_relative in links:
        target = _safe_child(data_root, target_relative)
        if not target.is_dir():
            continue
        source = user_home.joinpath(*PurePosixPath(relative).parts)
        # Existing valid links may be retained. Deploy must verify them itself.
        if _reparse(source) and source.resolve() == target.resolve():
            status = "already_linked"
        else:
            status = "existing" if source.exists() or _reparse(source) else "create_if_absent"
        result["link_plan"].append({"source": str(source), "target": str(target), "status": status})
        if status == "existing":
            result["conflicts"].append({"kind": "native_entry", "path": str(source), "resolution": "deploy_must_check_existing"})
    mirrors = [("native/workbuddy/legacy/MEMORY.md", ".workbuddy/MEMORY.md")]
    accounts = data_root / "native/workbuddy/accounts"
    if accounts.is_dir():
        mirrors.extend((p.relative_to(data_root).as_posix(), ".workbuddy/" + p.parent.name + "/MEMORY.md")
                       for p in accounts.glob("user-*/MEMORY.md"))
    for native_relative, user_relative in mirrors:
        target = _safe_child(data_root, native_relative)
        if target.is_file():
            result["file_copy_plan"].append({"source": str(target),
                                            "target": str(user_home.joinpath(*PurePosixPath(user_relative).parts)),
                                            "policy": "copy_only_if_absent"})
    result["complete"] = not result["errors"] and not result["conflicts"]
    result["report_path"] = str(report_dir / "restore-report.json")
    _write_new(Path(result["report_path"]), _serialize(result))
    return result

