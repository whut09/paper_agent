import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_agent.evaluation.runner import evaluate_case, evaluate_cases, load_cases


def test_evaluation_runner_loads_cases_and_reports_metrics():
    with TemporaryDirectory() as temp_dir:
        case_path = Path(temp_dir) / "case.json"
        case_path.write_text(
            json.dumps(
                {
                    "name": "minimal",
                    "input_text": (
                        "1 Introduction\nThis paper studies grounded evaluation for paper reading agents and traceable reports.\n\n"
                        "2 Method\nThe method uses evidence ids for claim verification.\n\n"
                        "3 Experiments\nExperiments report pass rates across multiple synthetic golden cases."
                    ),
                    "assets": [],
                    "expected_claims": ["The method uses evidence ids for claim verification."],
                    "forbidden_claims": ["The paper introduces a new benchmark dataset."],
                    "expected_sections": ["intro", "method", "experiments"],
                }
            ),
            encoding="utf-8",
        )

        cases = load_cases(temp_dir)
        result = evaluate_case(cases[0])
        report = evaluate_cases(temp_dir)

    assert result.metrics.grounded_claim_rate == 1.0
    assert result.metrics.unsupported_core_claim_count == 1
    assert result.metrics.coverage_score == 1.0
    assert result.metrics.asset_reference_accuracy == 1.0
    assert set(report["metrics"]) == {
        "grounded_claim_rate",
        "unsupported_core_claim_count",
        "coverage_score",
        "asset_reference_accuracy",
    }


def test_bundled_golden_cases_are_evaluable():
    report = evaluate_cases("evaluation/golden_cases")

    assert report["case_count"] == 3
    assert report["metrics"]["grounded_claim_rate"] >= 0.9
    assert report["metrics"]["coverage_score"] == 1.0
    assert report["metrics"]["asset_reference_accuracy"] == 1.0


def test_eval_cli_runs_before_translation_pipeline_loads():
    from paper_agent.paper_agent import main, parse_args

    parsed = parse_args(["eval", "--cases", "evaluation/golden_cases"])
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(["eval", "--cases", "evaluation/golden_cases"])

    assert parsed.command == "eval"
    assert parsed.cases == "evaluation/golden_cases"
    assert exit_code == 0
    assert '"grounded_claim_rate"' in output.getvalue()
    assert "paper_agent.doclayout" not in sys.modules
