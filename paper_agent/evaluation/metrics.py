"""Evaluation metric placeholders for future harness scoring."""


def pass_rate(passed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return passed / total


__all__ = ["pass_rate"]

