"""Run the selected deterministic v3 against the frozen synthetic AF-025 set."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = Path(__file__).resolve().parent / "v2"
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_baseline as evaluator  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    annotation_path = TASK / "annotation.json"
    frozen_path = TASK / "frozen_manifest.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    for rel, expected_hash in frozen["input_sha256"].items():
        if sha(ROOT / rel) != expected_hash:
            raise SystemExit(f"frozen input changed: {rel}")
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    evaluator.FIXTURES = TASK / "fixtures"
    slices = []
    for item in annotation["cases"]:
        expected = item["expected"]
        expected_tuple = (expected["type"], expected["anchor"], tuple(expected["evidence"]))
        slices.append(("holdout", item["chat"], "20T23:00", (expected_tuple,)))
    evaluator.SLICES = tuple(slices)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        evaluator.main()
    raw = json.loads(output.getvalue())
    files = sorted([annotation_path, *TASK.joinpath("fixtures").glob("*.json")])
    result = {
        "dataset_id": annotation["dataset_id"],
        "authorship": annotation["authorship"],
        "development_overlap": annotation["development_overlap"],
        "frozen_input_sha256": frozen["input_sha256"],
        "configuration": {
            "commit": __import__("subprocess").check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "model": "local-baseline-unanswered-v1",
            "unanswered_detector": "local-unanswered-v3",
            "prompt_rules": "unanswered-rules-v3",
            "commitment_detector": "local-commitment-v1",
            "complaint_detector": "local-complaint-v1",
            "context": "baseline; Cognee excluded (issue #23 requires fixes)",
            "cost_usd": None,
            "external_cost_usd": 0,
        },
        "metrics": raw["metrics"]["holdout"],
        "slices": raw["slices"],
        "protocol_thresholds": {"precision": 0.8, "recall": 0.9, "valid_complete_evidence": 1.0,
                                 "sla_errors": 0, "duplicates": 0, "critical_misses": 0,
                                 "cost_usd": "must be known or separately approved"},
        "assessment": "diagnostic synthetic evaluation; agent-authored labels; not real-world pilot evidence",
    }
    result_path = TASK / "results.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=lambda x: dict(x)) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2, default=lambda x: dict(x)))


if __name__ == "__main__":
    main()
