"""Thread-safe kernel registry for hot-pluggable kernel switching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paper_agent.kernel.protocol import KernelProtocol


class KernelRegistry:
    _lock = threading.RLock()
    _active: KernelProtocol | None = None
    _kernels: dict[str, KernelProtocol] = {}

    @classmethod
    def register(cls, kernel: KernelProtocol) -> None:
        with cls._lock:
            cls._kernels[kernel.name] = kernel

    @classmethod
    def get(cls, name: str | None = None) -> KernelProtocol:
        with cls._lock:
            if name:
                return cls._kernels[name]
            if cls._active:
                return cls._active
            return cls._kernels["fast"]

    @classmethod
    def switch(cls, name: str) -> None:
        with cls._lock:
            kernel = cls._kernels[name]
            if hasattr(kernel, "ensure_venv"):
                kernel.ensure_venv()  # type: ignore[attr-defined]
            if not kernel.is_available():
                raise RuntimeError(
                    f"Kernel '{name}' is not available. "
                    "Check that the submodule is initialized and venv is set up."
                )
            cls._active = kernel

    @classmethod
    def active_name(cls) -> str:
        with cls._lock:
            return cls._active.name if cls._active else "fast"

    @classmethod
    def available(cls) -> list[str]:
        with cls._lock:
            return [n for n, k in cls._kernels.items() if k.is_available()]

    @classmethod
    def _reset(cls) -> None:
        """Reset registry state. For testing only."""
        with cls._lock:
            cls._active = None
            cls._kernels.clear()
