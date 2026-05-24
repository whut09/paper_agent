"""Kernel package — hot-pluggable translation kernel registry."""

from paper_agent.kernel.registry import KernelRegistry
from paper_agent.kernel.legacy import LegacyKernel
from paper_agent.kernel.precise import PreciseKernel

# Always register both kernels.
# PreciseKernel.is_available() returns False if submodule/venv not set up.
KernelRegistry.register(LegacyKernel())
KernelRegistry.register(PreciseKernel())

__all__ = ["KernelRegistry"]
