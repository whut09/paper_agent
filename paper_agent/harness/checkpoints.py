"""Content-addressed, atomic workflow checkpoints.

The checkpoint store deliberately persists only data owned by the workflow
context. Runtime handles such as clients, callbacks, cancellation events and
configuration dictionaries are excluded so secrets never reach disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paper_agent


CHECKPOINT_SCHEMA_VERSION = "paper-agent-checkpoint-v1"
PROMPT_VERSION = "paper-summary-v5"
_SECRET_RE = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL)", re.IGNORECASE)
_RUNTIME_FIELDS = {
    "client",
    "config",
    "codex_envs",
    "progress",
    "cancellation_event",
    "node_cancellation_events",
    "node_deadlines",
    "node_attempts",
    "node_results",
    "agent_trace",
    "run_id",
}
_BEHAVIOR_CONFIG_KEYS = (
    "CODEX_BASE_URL",
    "CODEX_MODEL",
    "CODEX_USE_PROXY",
    "CODEX_PROXY",
    "CODEX_TIMEOUT_SECONDS",
    "CODEX_CHAT_ATTEMPTS",
    "CODEX_SUMMARY_CONCURRENCY",
    "CODEX_CHUNK_CHARS",
    "CODEX_STREAM_TIMEOUT_SECONDS",
    "PAPER_AGENT_WORKFLOW_TIMEOUT_SECONDS",
    "PAPER_AGENT_NODE_TIMEOUT_SECONDS",
)


class CheckpointValidationError(RuntimeError):
    """Raised when a checkpoint is missing, corrupt or no longer usable."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
            if not _SECRET_RE.search(item.name)
        }
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _SECRET_RE.search(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {str(key): _jsonable(item) for key, item in sorted(value.__dict__.items()) if not _SECRET_RE.search(str(key))}
    return repr(value)


def _secret_free_config(values: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _jsonable(_sanitize_config_value(value))
        for key, value in sorted(values.items())
        if not _SECRET_RE.search(str(key))
    }


def _sanitize_config_value(value: Any) -> Any:
    if not isinstance(value, str) or "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _context_value(context: Any, name: str) -> Any:
    aliases = {
        "paper_text": "text",
        "asset_manifest": "assets",
        "draft_report": "summary",
        "verification_report": "verification",
        "verified_report": "summary",
        "docx": "docx_path",
        "summary.md": "summary_markdown_path",
    }
    return getattr(context, aliases.get(name, name), None)


def context_state(context: Any) -> dict[str, Any]:
    """Return the persistable state of a workflow context."""

    if not is_dataclass(context):
        values = vars(context)
    else:
        values = {item.name: getattr(context, item.name) for item in fields(context)}
    return {
        name: value
        for name, value in values.items()
        if name not in _RUNTIME_FIELDS
        and not name.startswith("_")
        and name not in {"checkpoint_root", "checkpoint_keys", "restored_nodes", "invalidated_nodes"}
    }


def restore_context(context: Any, state: dict[str, Any]) -> None:
    for name, value in state.items():
        if name not in _RUNTIME_FIELDS and name not in {"checkpoint_root", "checkpoint_keys", "restored_nodes", "invalidated_nodes"} and hasattr(context, name):
            setattr(context, name, value)


def identity_for_context(context: Any) -> dict[str, Any]:
    input_path = Path(str(getattr(context, "input_path", "")))
    pdf_path = getattr(context, "pdf_path", None)
    source = Path(str(pdf_path)) if pdf_path else input_path
    try:
        source_hash = file_sha256(source) if source.is_file() else hashlib.sha256(str(source).encode()).hexdigest()
    except OSError:
        source_hash = hashlib.sha256(str(source).encode()).hexdigest()
    pages = getattr(context, "pages", None)
    normalized_pages = sorted({int(page) for page in pages}) if pages is not None else None
    envs = getattr(context, "codex_envs", {}) or {}
    config_values = dict(envs) if isinstance(envs, dict) else {}
    try:
        from paper_agent.config import ConfigManager

        for name in _BEHAVIOR_CONFIG_KEYS:
            if name not in config_values:
                value = ConfigManager.get(name)
                if value is not None:
                    config_values[name] = value
    except (OSError, ValueError):
        pass
    config_identity = _secret_free_config(config_values)
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "source_pdf_sha256": source_hash,
        "pages": normalized_pages,
        "max_assets": int(getattr(context, "max_assets", 0) or 0),
        "prompt_version": str(getattr(context, "prompt_version", "") or PROMPT_VERSION),
        "model_config": config_identity,
        "code_version": getattr(paper_agent, "__version__", "unknown"),
    }


def stable_digest(value: Any) -> str:
    payload = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def node_key(
    context: Any,
    node_name: str,
    dependency_keys: dict[str, str],
    required_inputs: tuple[str, ...] = (),
) -> str:
    identity = identity_for_context(context)
    relevant = {name: _context_value(context, name) for name in required_inputs}
    payload = {
        "identity": identity,
        "node": node_name,
        "dependencies": dict(sorted(dependency_keys.items())),
        "relevant": relevant,
    }
    return stable_digest(payload)


class CheckpointStore:
    def __init__(self, output_dir: str | Path, identity: dict[str, Any]):
        self.identity = identity
        self.base_key = stable_digest(identity)
        self.root = Path(output_dir) / ".paper-agent-checkpoints" / self.base_key
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, node_name: str, key: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_name)
        return self.root / f"{safe_name}-{key}.ckpt"

    def save(self, node_name: str, key: str, state: dict[str, Any], result: Any) -> Path:
        payload = pickle.dumps({"state": state, "result": result}, protocol=pickle.HIGHEST_PROTOCOL)
        envelope = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "identity": self.identity,
            "base_key": self.base_key,
            "node": node_name,
            "key": key,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "artifacts": [str(item) for item in getattr(result, "artifacts", ())],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        target = self._path(node_name, key)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as handle:
                pickle.dump(envelope, handle, protocol=pickle.HIGHEST_PROTOCOL)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    def load(self, node_name: str, key: str) -> tuple[dict[str, Any], Any] | None:
        path = self._path(node_name, key)
        if not path.exists():
            return None
        try:
            with path.open("rb") as handle:
                envelope = pickle.load(handle)
            if envelope.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                raise CheckpointValidationError("checkpoint schema version mismatch")
            if envelope.get("base_key") != self.base_key or envelope.get("key") != key or envelope.get("node") != node_name:
                raise CheckpointValidationError("checkpoint identity mismatch")
            payload = envelope.get("payload")
            if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != envelope.get("payload_sha256"):
                raise CheckpointValidationError("checkpoint payload digest mismatch")
            for artifact in envelope.get("artifacts", ()):
                if artifact and not Path(artifact).exists():
                    raise CheckpointValidationError(f"checkpoint artifact missing: {artifact}")
            decoded = pickle.loads(payload)
            return decoded["state"], decoded["result"]
        except CheckpointValidationError:
            raise
        except (OSError, EOFError, KeyError, TypeError, ValueError, AttributeError, IndexError, pickle.PickleError) as exc:
            raise CheckpointValidationError(f"checkpoint unreadable: {exc}") from exc


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "CheckpointStore",
    "CheckpointValidationError",
    "context_state",
    "file_sha256",
    "identity_for_context",
    "node_key",
    "restore_context",
    "stable_digest",
]
