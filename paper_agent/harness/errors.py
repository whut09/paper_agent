"""Error taxonomy used by the workflow execution loop."""

from __future__ import annotations

import asyncio
import re


def model_request_rejection_code(exc: BaseException) -> str:
    """Identify terminal gateway denials even inside workflow wrappers."""
    pending = [exc]
    seen: set[int] = set()
    access_denied = False
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        text = str(current).lower()
        if "request_check_blocked" in text or "model_request_blocked" in text:
            return "model_request_blocked"
        if getattr(current, "status_code", None) in {401, 403} or re.search(r"\bHTTP\s+(?:401|403)\b", text, re.I):
            access_denied = True
    return "model_access_denied" if access_denied else ""


class WorkflowError(RuntimeError):
    recoverable = False


class PaperAgentHarnessError(WorkflowError):
    """Backward-compatible base error for harness orchestration failures."""


class VerificationBlockedError(PaperAgentHarnessError):
    """Raised when verified claims should block report generation."""


class RecoverableWorkflowError(WorkflowError):
    recoverable = True


class NonRecoverableWorkflowError(WorkflowError):
    recoverable = False


class NodeTimeoutError(RecoverableWorkflowError):
    pass


class WorkflowTimeoutError(RecoverableWorkflowError):
    pass


def is_recoverable_error(exc: BaseException) -> bool:
    if model_request_rejection_code(exc):
        return False
    # Nodes often wrap an SDK exception in a user-facing RuntimeError.  The
    # old classifier only inspected the wrapper text, so a Chinese wrapper
    # around APIConnectionError was incorrectly treated as non-recoverable.
    # Walk the complete exception chain before applying message heuristics.
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        if isinstance(current, (RecoverableWorkflowError, TimeoutError, ConnectionError, OSError)):
            return True
        text = str(current).lower()
        if any(
            marker in text
            for marker in (
                "timeout",
                "timed out",
                "connection",
                "连接失败",
                "服务端断开",
                "temporarily unavailable",
                "server disconnected",
                "rate limit",
                "429",
                "502",
                "503",
                "504",
                "524",
                "transport",
            )
        ):
            return True
    return False


def classify_error(exc: BaseException) -> str:
    """Return a stable routing label for trace and retry policy."""

    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "cancelled"
    return "recoverable" if is_recoverable_error(exc) else "nonrecoverable"


__all__ = [
    "NodeTimeoutError",
    "NonRecoverableWorkflowError",
    "PaperAgentHarnessError",
    "RecoverableWorkflowError",
    "VerificationBlockedError",
    "WorkflowError",
    "WorkflowTimeoutError",
    "classify_error",
    "is_recoverable_error",
    "model_request_rejection_code",
]
