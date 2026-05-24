"""Convert doc/docx files to PDF using LibreOffice headless."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".doc", ".docx"}


def is_convertible(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def convert_to_pdf(input_path: str) -> str:
    """Convert a doc/docx file to PDF using LibreOffice.

    Returns the path to the generated temporary PDF file.
    The caller is responsible for cleaning up the temp file.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError(
            "LibreOffice is required to convert doc/docx files. "
            "Install it with: apt-get install libreoffice-core (Linux) "
            "or brew install --cask libreoffice (macOS)"
        )

    p = Path(input_path)
    tmpdir = tempfile.mkdtemp(prefix="paper_agent_docx_")

    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmpdir, str(p)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

    pdf_path = Path(tmpdir) / f"{p.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError(f"Conversion produced no output. Expected: {pdf_path}")

    logger.info(f"Converted {p.name} -> {pdf_path}")
    return str(pdf_path)
