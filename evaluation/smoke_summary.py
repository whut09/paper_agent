"""Run a real paper with the same configuration as the GUI, without mocks."""

import argparse
import json
import sys
import zipfile
from xml.etree import ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_agent.config import ConfigManager
from paper_agent.harness.workflow import summarize_paper_detailed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--paper-url", default="")
    parser.add_argument("--max-assets", type=int, default=13)
    parser.add_argument("--check-existing", action="store_true",
                        help="Only recheck artifacts of a completed real run")
    args = parser.parse_args()
    ConfigManager.custome_config(args.config)
    config = ConfigManager.all()
    envs = {key: str(config.get(key, "")) for key in (
        "CODEX_BASE_URL", "CODEX_API_KEY", "CODEX_MODEL", "CODEX_USE_PROXY",
        "CODEX_PROXY", "CODEX_WIRE_API",
    )}
    print(json.dumps({"model": envs["CODEX_MODEL"],
                      "wire_api": envs["CODEX_WIRE_API"] or "responses",
                      "output": args.output}, ensure_ascii=False), flush=True)

    def progress(value: float, desc: str) -> None:
        print(f"{value:.0%} {desc}", flush=True)

    if args.check_existing:
        from paper_agent.schemas.qa import SummaryRunResult

        output = Path(args.output)
        name = Path(args.input).stem
        acceptance = json.loads((output / f"{name}-acceptance.json").read_text(encoding="utf-8"))
        trace = json.loads((output / f"{name}-trace.json").read_text(encoding="utf-8"))
        if acceptance.get("status") != "passed" or not trace.get("download_ready"):
            print("FAIL: existing run did not pass acceptance/download checks", flush=True)
            return 1
        result = SummaryRunResult(status="success", message="Existing real run accepted",
                                  docx_path=output / f"{name}-summary.docx", downloadable=True)
    else:
        result = summarize_paper_detailed(
            args.input, args.output, summary_language="中文", codex_envs=envs,
            max_assets=args.max_assets, paper_url=args.paper_url, progress=progress,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), flush=True)
    if not result.downloadable or not result.docx_path or not result.docx_path.is_file():
        return 1
    if args.paper_url:
        with zipfile.ZipFile(result.docx_path) as doc:
            root = ElementTree.fromstring(doc.read("word/document.xml"))
        text = "".join(node.text or "" for node in root.iter(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        if args.paper_url not in text:
            print("FAIL: input paper URL missing from Word report", flush=True)
            return 1
    # Exercise the same result-to-download adapter used by the GUI callback.
    from paper_agent.gui import _summary_callback_response

    updates = _summary_callback_response(result, args.input)
    if updates[0].get("value") != str(result.docx_path) or not updates[0].get("visible"):
        print("FAIL: GUI download adapter rejected the generated report", flush=True)
        return 1
    print("PASS: real model generation, Word paper URL, GUI download adapter", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
