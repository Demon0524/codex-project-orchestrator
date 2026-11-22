"""Immutable structured checkpoints and recoverable handoff phase records.

Validation/rendering is reused from session-handoff (MIT). The phase journal is
an independent implementation informed by its durable supervisor transitions;
see THIRD_PARTY_NOTICES.md for upstream pinning and attribution.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Callable

from vendor.session_handoff_state import MAX_CONTENT_BYTES, render_state, validate_state


class CheckpointError(ValueError):
    """Invalid, changed or unavailable checkpoint."""


_REF = re.compile(r"checkpoint://([0-9a-f]{64})\Z")
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-(?:proj-)?[A-Za-z0-9_-]{16,})\b")
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]{12,}=*")
_ASSIGNMENT = re.compile(r'''(?ix)(\b(?:password|passwd|api[_-]?key|access[_-]?token|secret)\b\s*[=:]\s*)(?:"[^"\r\n]+"|'[^'\r\n]+'|[^\s,;]+)''')
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.DOTALL)


def redact_common_credentials(text: str) -> tuple[str, int]:
    """Best-effort filtering of common tokens; never a completeness guarantee."""
    count = 0
    for pattern, replacement in ((_PRIVATE_KEY, "[REDACTED PRIVATE KEY]"),
                                 (_TOKEN, "[REDACTED TOKEN]"),
                                 (_BEARER, r"\1[REDACTED]"),
                                 (_ASSIGNMENT, r"\1[REDACTED]")):
        text, found = pattern.subn(replacement, text)
        count += found
    return text, count


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _private_write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _regular_read(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CheckpointError("Checkpoint component must be a regular file")
            content = stream.read(limit + 1)
    except OSError as exc:
        raise CheckpointError("Checkpoint component unavailable or unsafe") from exc
    if len(content) > limit:
        raise CheckpointError("Checkpoint component exceeds size limit")
    return content


class CheckpointStore:
    """Keep JSON and its canonical Markdown in immutable, atomically placed dirs.

    The request ID selects a stable reference. Reusing it with different raw
    input is an error, even when both inputs redact to the same output.
    """
    def __init__(self, directory, redactor: Callable[[str], tuple[str, int]] | None = None):
        self.directory = Path(directory).expanduser().absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise CheckpointError("Checkpoint directory must not be a symlink")
        self.directory = self.directory.resolve()
        self.redactor = redactor or redact_common_credentials

    def _redact(self, state: dict) -> tuple[dict, int]:
        count = 0

        def visit(value):
            nonlocal count
            if isinstance(value, str):
                result, replaced = self.redactor(value)
                if not isinstance(result, str) or type(replaced) is not int or replaced < 0:
                    raise CheckpointError("Redactor returned invalid output")
                count += replaced
                return result
            if isinstance(value, list):
                return [visit(item) for item in value]
            if isinstance(value, dict):
                return {key: visit(item) for key, item in value.items()}
            return value

        return validate_state(visit(state)), count

    def create(self, request_id: str, state: object) -> dict:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512 or "\x00" in request_id:
            raise CheckpointError("request_id must be a nonblank string of at most 512 characters")
        normalized = validate_state(state)
        raw_hash = hashlib.sha256(_canonical(normalized)).hexdigest()
        filtered, redactions = self._redact(normalized)
        markdown = render_state(filtered).encode("utf-8")
        identifier = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        ref = "checkpoint://" + identifier
        fd = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            target = self.directory / identifier
            if target.exists() or target.is_symlink():
                existing = self.read(ref)
                if existing["request_fingerprint"] != raw_hash:
                    raise CheckpointError("request_id reused with different checkpoint content")
                return {**existing, "replayed": True}
            payload = {"schema_version": 1, "checkpoint_ref": ref,
                       "request_fingerprint": raw_hash,
                       "sha256": hashlib.sha256(markdown).hexdigest(),
                       "created_at": _utcnow(), "redactions": redactions, "state": filtered}
            staging = Path(tempfile.mkdtemp(prefix=".checkpoint-", dir=self.directory))
            try:
                _private_write(staging / "record.json", _canonical(payload))
                _private_write(staging / "checkpoint.md", markdown)
                staged = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(staged)
                finally:
                    os.close(staged)
                # Both files become visible together. The lock prevents replacement.
                os.rename(staging, target)
                parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return self.read(ref)

    def read(self, checkpoint_ref: str) -> dict:
        match = _REF.fullmatch(checkpoint_ref) if isinstance(checkpoint_ref, str) else None
        if not match:
            raise CheckpointError("Invalid checkpoint reference")
        folder = self.directory / match.group(1)
        if folder.is_symlink() or not folder.is_dir():
            raise CheckpointError("Checkpoint directory unavailable or unsafe")
        if folder.resolve().parent != self.directory:
            raise CheckpointError("Checkpoint reference escaped storage directory")
        try:
            record = json.loads(_regular_read(folder / "record.json", MAX_CONTENT_BYTES + 16384))
            if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("checkpoint_ref") != checkpoint_ref:
                raise CheckpointError("Checkpoint metadata is inconsistent")
            normalized = validate_state(record["state"])
            content = _regular_read(folder / "checkpoint.md", MAX_CONTENT_BYTES + 65536)
            if hashlib.sha256(content).hexdigest() != record.get("sha256") or content != render_state(normalized).encode("utf-8"):
                raise CheckpointError("Checkpoint content was changed")
            if not re.fullmatch(r"[0-9a-f]{64}", record.get("request_fingerprint", "")):
                raise CheckpointError("Checkpoint request fingerprint is invalid")
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError("Invalid checkpoint record") from exc
        return {**record, "path": str(folder / "checkpoint.md")}


PHASES = {"prepared", "creating", "verification_pending", "accepted",
          "activation_sending", "active", "blocked"}
_TRANSITIONS = {
    "prepared": {"creating", "blocked"},
    "creating": {"verification_pending", "blocked"},
    "verification_pending": {"accepted", "blocked"},
    "accepted": {"activation_sending", "blocked"},
    "activation_sending": {"active", "blocked"},
    "active": set(),
    "blocked": {"prepared", "verification_pending", "activation_sending"},
}


def transition(record: dict, phase: str, **fields) -> dict:
    """Return a new phase record; callers persist it under their own transaction.

    A blocked activation cannot be replayed automatically. Recovery to prepared
    or verification_pending is explicit. Recovering activation_sending requires
    both the original activation request ID and a caller-verified real turn ID;
    this helper does not itself establish that evidence. No process is started
    or stopped by this pure helper.
    """
    if not isinstance(record, dict) or phase not in PHASES:
        raise CheckpointError("Invalid transition record or phase")
    current = record.get("phase")
    if current is None:
        if phase != "prepared":
            raise CheckpointError("A journal must begin with prepared")
    elif current not in PHASES or (phase != current and phase not in _TRANSITIONS[current]):
        raise CheckpointError(f"Illegal handoff phase transition: {current} -> {phase}")
    if current == "blocked" and phase == "activation_sending":
        request_id = record.get("activation_request_id")
        turn_id = fields.get("reconciled_activation_turn_id")
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(turn_id, str) or not turn_id.strip():
            raise CheckpointError("Activation recovery requires the original request ID and a reconciled real turn ID")
    if any(key in fields for key in ("phase", "history", "updated_at")):
        raise CheckpointError("Reserved journal fields cannot be overwritten")
    updated = copy.deepcopy(record)
    updated.update(copy.deepcopy(fields))
    updated["phase"] = phase
    updated["updated_at"] = _utcnow()
    history = list(updated.get("history", []))
    if current != phase:
        history.append({"from": current, "to": phase, "at": updated["updated_at"]})
    updated["history"] = history[-32:]
    return updated
