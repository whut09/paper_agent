"""Error taxonomy used by the workflow execution loop."""

from __future__ import annotations

import asyncio


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
    if isinstance(exc, BaseExceptionGroup):
        return any(is_recoverable_error(item) for item in exc.exceptions)
    if isinstance(exc, (RecoverableWorkflowError, TimeoutError, ConnectionError, OSError)):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "server disconnected",
            "rate limit",
            "429",
            "502",
            "503",
            "504",
            "transport",
        )
    )


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
]
