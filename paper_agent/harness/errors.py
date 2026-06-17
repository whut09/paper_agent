"""Harness-level errors."""


class PaperAgentHarnessError(RuntimeError):
    """Base error for PaperAgent harness orchestration failures."""


class VerificationBlockedError(PaperAgentHarnessError):
    """Raised when verified claims should block report generation."""


__all__ = ["PaperAgentHarnessError", "VerificationBlockedError"]

