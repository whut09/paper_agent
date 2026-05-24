"""Precise kernel adapter — runs paper_agent_next in an isolated subprocess/venv."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from paper_agent.kernel.v2_bridge import request_to_cli_args, request_to_env
from paper_agent.kernel.protocol import TranslateRequest, TranslateResult

logger = logging.getLogger(__name__)

# Resolve paths relative to the kernel package directory
_SUBMODULE_DIR = Path(__file__).resolve().parent / "PaperAgent-next.git"
_VENV_DIR = _SUBMODULE_DIR / ".venv"
_WORKER_SCRIPT = Path(__file__).resolve().parent / "v2_worker.py"


def _venv_python() -> str:
    """Return the path to the venv's Python interpreter."""
    if sys.platform == "win32":
        return str(_VENV_DIR / "Scripts" / "python.exe")
    return str(_VENV_DIR / "bin" / "python")


class PreciseKernel:
    """Kernel adapter for paper_agent_next, running in a subprocess with isolated deps."""

    @property
    def name(self) -> str:
        return "precise"

    @property
    def version(self) -> str:
        if not self.is_available():
            return "unknown"
        try:
            result = subprocess.run(
                [
                    _venv_python(),
                    "-c",
                    "import paper_agent_next; print(paper_agent_next.__version__)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(_SUBMODULE_DIR),
            )
            return result.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    def is_available(self) -> bool:
        """Check if submodule exists and venv is initialized."""
        return (
            _SUBMODULE_DIR.is_dir()
            and (_SUBMODULE_DIR / "pyproject.toml").exists()
            and Path(_venv_python()).exists()
        )

    def ensure_venv(self) -> None:
        """Create venv and install paper_agent_next if not already set up."""
        if (
            not _SUBMODULE_DIR.is_dir()
            or not (_SUBMODULE_DIR / "pyproject.toml").exists()
        ):
            raise RuntimeError(
                "PaperAgent-next submodule not found. "
                "Run: git submodule update --init paper_agent/kernel/PaperAgent-next.git"
            )

        venv_exists = Path(_venv_python()).exists()

        if venv_exists and self._package_importable():
            return

        if not venv_exists:
            logger.info("Creating precise kernel venv...")
            subprocess.run(
                [sys.executable, "-m", "venv", str(_VENV_DIR)],
                check=True,
                timeout=60,
            )

        logger.info("Installing paper_agent_next into venv...")
        subprocess.run(
            [_venv_python(), "-m", "pip", "install", "-e", str(_SUBMODULE_DIR)],
            check=True,
            timeout=300,
            cwd=str(_SUBMODULE_DIR),
        )

        logger.info("Precise kernel venv ready.")

    def _package_importable(self) -> bool:
        """Check if paper_agent_next can be imported in the venv."""
        try:
            result = subprocess.run(
                [_venv_python(), "-c", "import paper_agent_next"],
                capture_output=True,
                timeout=30,
                cwd=str(_SUBMODULE_DIR),
                env={**os.environ, "PYTHONPATH": str(_SUBMODULE_DIR)},
            )
            return result.returncode == 0
        except Exception:
            return False

    def _build_subprocess_env(self, request: TranslateRequest) -> dict[str, str]:
        """Build environment for the subprocess with PAPER_AGENT_ prefixed vars."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_SUBMODULE_DIR)
        env.update(request_to_env(request))
        return env

    def translate(
        self,
        request: TranslateRequest,
        callback: Any = None,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> list[TranslateResult]:
        self.ensure_venv()

        cli_args = request_to_cli_args(request)
        input_json = json.dumps(cli_args)
        env = self._build_subprocess_env(request)

        cmd = [_venv_python(), str(_WORKER_SCRIPT)]
        logger.info("Starting precise kernel subprocess: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_SUBMODULE_DIR),
            env=env,
        )

        stderr_lines: list[str] = []
        try:
            assert proc.stdin is not None
            assert proc.stderr is not None
            assert proc.stdout is not None
            proc.stdin.write(input_json)
            proc.stdin.close()

            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("not a JSON object")
                    if callback:
                        callback(event)
                except (json.JSONDecodeError, ValueError):
                    print(line, file=sys.stderr, flush=True)

            stdout = proc.stdout.read()
            proc.wait()

        except Exception:
            proc.kill()
            raise

        if proc.returncode != 0:
            detail = "\n".join(stderr_lines[-20:]) if stderr_lines else "(no stderr)"
            raise RuntimeError(
                f"Precise kernel subprocess failed (exit {proc.returncode}):\n{detail}"
            )

        try:
            result_data = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Invalid JSON from worker: {stdout[:200]}")

        results = []
        for r in result_data.get("results", []):
            results.append(
                TranslateResult(
                    mono_pdf=Path(r["mono_pdf"]) if r.get("mono_pdf") else None,
                    dual_pdf=Path(r["dual_pdf"]) if r.get("dual_pdf") else None,
                    time_cost=result_data.get("time_cost", 0.0),
                )
            )
        return results

    async def translate_async(
        self,
        request: TranslateRequest,
        callback: Any = None,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> list[TranslateResult]:
        self.ensure_venv()

        cli_args = request_to_cli_args(request)
        input_json = json.dumps(cli_args)
        env = self._build_subprocess_env(request)

        proc = await asyncio.create_subprocess_exec(
            _venv_python(),
            str(_WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_SUBMODULE_DIR),
            env=env,
        )

        stdout_bytes, stderr_bytes = await proc.communicate(input=input_json.encode())
        stdout = stdout_bytes.decode()

        if proc.returncode != 0:
            detail = stderr_bytes.decode()[-2000:] if stderr_bytes else "(no stderr)"
            raise RuntimeError(
                f"Precise kernel subprocess failed (exit {proc.returncode}):\n{detail}"
            )

        try:
            result_data = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Invalid JSON from worker: {stdout[:200]}")

        results = []
        for r in result_data.get("results", []):
            results.append(
                TranslateResult(
                    mono_pdf=Path(r["mono_pdf"]) if r.get("mono_pdf") else None,
                    dual_pdf=Path(r["dual_pdf"]) if r.get("dual_pdf") else None,
                    time_cost=result_data.get("time_cost", 0.0),
                )
            )
        return results


def setup_precise_cli() -> None:
    """CLI entry point: provision the precise kernel venv."""
    logging.basicConfig(level=logging.INFO)
    PreciseKernel().ensure_venv()
