"""Compare historical local unanswered rule configurations on development data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DETECTOR_PATH = "backend/src/actionflow/detectors/local_baseline.py"
EVALUATOR_PATH = ROOT / "scripts/evaluate_baseline.py"
CHALLENGE_PATH = Path(__file__).with_name("challenge_cases.json")
OUTPUT_PATH = (
    Path(__file__).parent / "results" / "af023-candidate-comparison-2026-11-23.json"
)
CANDIDATES = (
    {
        "name": "local-rules-v1",
        "commit": "ffff277",
        "prompt_version": "unanswered-rules-v1",
        "detector_version": "local-unanswered-v1",
    },
    {
        "name": "local-rules-v2",
        "commit": "274538c",
        "prompt_version": "unanswered-rules-v2",
        "detector_version": "local-unanswered-v2",
    },
    {
        "name": "local-rules-v3",
        "commit": "3dce17e",
        "prompt_version": "unanswered-rules-v3",
        "detector_version": "local-unanswered-v3",
    },
)


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def score_challenges(source: Path) -> dict:
    for name in tuple(sys.modules):
        if name == "actionflow" or name.startswith("actionflow."):
            del sys.modules[name]
    sys.path.insert(0, str(source))
    try:
        from actionflow.detectors.contracts import (  # noqa: PLC0415
            DetectorRequest,
            InputMessage,
        )
        from actionflow.detectors.local_baseline import (  # noqa: PLC0415
            LocalBaselineDetector,
        )

        challenge = json.loads(CHALLENGE_PATH.read_text(encoding="utf-8"))
        detector = LocalBaselineDetector()
        counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
        details = []
        epoch = datetime(2026, 9, 1, tzinfo=UTC)
        for index, item in enumerate(challenge["cases"]):
            messages = tuple(
                InputMessage(
                    message_id=message["id"],
                    revision_id=f"{message['id']}-r1",
                    external_id=message["id"],
                    text=message["text"],
                    sent_at=epoch + timedelta(minutes=10 * position),
                    role="window",
                    side=message["side"],
                    source_type="message",
                    reply_to_external_id=None,
                    attachments={},
                    author_name=None,
                )
                for position, message in enumerate(item["messages"])
            )
            request = DetectorRequest(
                run_id=f"af023-{item['id']}",
                project_id=f"synthetic-{index}",
                period_start=epoch,
                period_end=epoch + timedelta(hours=1),
                as_of=epoch + timedelta(hours=1),
                messages=messages,
            )
            result = detector.detect(request)
            actual = bool(result.candidates)
            expected = item["expected_unanswered"]
            key = (
                "tp"
                if expected and actual
                else "fn"
                if expected
                else "fp"
                if actual
                else "tn"
            )
            counts[key] += 1
            details.append(
                {
                    "case_id": item["id"],
                    "expected_unanswered": expected,
                    "actual_unanswered": actual,
                    "candidate_count": len(result.candidates),
                    "reason": item["reason"],
                    "cost_usd": (
                        str(result.usage.cost_usd)
                        if result.usage.cost_usd is not None
                        else None
                    ),
                }
            )
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        counts["precision"] = tp / (tp + fp) if tp + fp else None
        counts["recall"] = tp / (tp + fn) if tp + fn else None
        return {"metrics": counts, "cases": details}
    finally:
        sys.path.pop(0)
        for name in tuple(sys.modules):
            if name == "actionflow" or name.startswith("actionflow."):
                del sys.modules[name]


def run_candidate(candidate: dict) -> dict:
    candidate_commit = git_output("rev-parse", candidate["commit"])
    source_bytes = subprocess.run(
        ["git", "show", f"{candidate_commit}:{DETECTOR_PATH}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="af023-candidate-") as temporary:
        package_root = Path(temporary) / "source"
        shutil.copytree(ROOT / "backend/src/actionflow", package_root / "actionflow")
        detector_file = package_root / "actionflow/detectors/local_baseline.py"
        detector_file.write_bytes(source_bytes)

        runner = Path(temporary) / "run_development.py"
        runner.write_text(
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('af020', {str(EVALUATOR_PATH)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.SLICES = tuple(\n"
            "    item for item in module.SLICES if item[0] == 'development'\n"
            ")\n"
            "module.main()\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(package_root), str(ROOT / "backend/src"))
        )
        process = subprocess.run(
            [sys.executable, str(runner)],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        evaluation = json.loads(process.stdout)
        evaluation["metrics"] = evaluation["metrics"]["development"]
        return {
            "name": candidate["name"],
            "source_commit": candidate_commit,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "prompt_version": candidate["prompt_version"],
            "detector_version": candidate["detector_version"],
            "model_id": "local-baseline-unanswered-v1",
            "model_params": {
                "strategy": "rules",
                "version": candidate["detector_version"],
            },
            "metrics": evaluation["metrics"],
            "slices": evaluation["slices"],
            "synthetic_challenges": score_challenges(package_root),
        }


def stable_result_hash(result: dict) -> str:
    stable = {
        "metrics": result["metrics"],
        "slices": [
            {
                "split": item["split"],
                "chat": item["chat"],
                "as_of": item["as_of"],
                "counts": item["counts"],
                "input_limitations": item["input_limitations"],
            }
            for item in result["slices"]
        ],
        "challenge_metrics": result["synthetic_challenges"]["metrics"],
        "challenge_predictions": [
            {
                "case_id": item["case_id"],
                "expected_unanswered": item["expected_unanswered"],
                "actual_unanswered": item["actual_unanswered"],
                "candidate_count": item["candidate_count"],
            }
            for item in result["synthetic_challenges"]["cases"]
        ],
    }
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> None:
    results = []
    for candidate in CANDIDATES:
        first = run_candidate(candidate)
        repeated = run_candidate(candidate)
        first_hash = stable_result_hash(first)
        repeated_hash = stable_result_hash(repeated)
        first["stable_result_sha256"] = first_hash
        first["repeat_verification"] = {
            "repeated_stable_result_sha256": repeated_hash,
            "matches": first_hash == repeated_hash,
        }
        results.append(first)
    files = sorted((ROOT / "data/fixtures/telegram").glob("*.json"))
    artifact = {
        "metadata": {
            "af_id": "AF-023 part 1",
            "evaluated_at": "2026-11-23",
            "current_commit": git_output("rev-parse", "HEAD"),
            "development_only": True,
            "holdout_executed": False,
            "candidates": [candidate["name"] for candidate in CANDIDATES],
            "evaluator_sha256": hashlib.sha256(EVALUATOR_PATH.read_bytes()).hexdigest(),
            "annotation_sha256": hashlib.sha256(
                (ROOT / "docs/experiment/af-002-annotation.md").read_bytes()
            ).hexdigest(),
            "fixture_sha256": {
                str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in files
            },
            "challenge_sha256": hashlib.sha256(CHALLENGE_PATH.read_bytes()).hexdigest(),
            "uv_lock_sha256": hashlib.sha256(
                (ROOT / "backend/uv.lock").read_bytes()
            ).hexdigest(),
        },
        "results": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = [
        {
            "candidate": item["name"],
            "model_id": item["model_id"],
            "prompt_version": item["prompt_version"],
            "dev_unanswered": {
                key: item["metrics"]["unanswered"][key]
                for key in ("tp", "fp", "fn", "precision", "recall")
            },
            "challenge": item["synthetic_challenges"]["metrics"],
            "repeat_matches": item["repeat_verification"]["matches"],
            "stable_result_sha256": item["stable_result_sha256"],
        }
        for item in results
    ]
    print(
        json.dumps(
            {
                "output": str(OUTPUT_PATH.relative_to(ROOT)).replace("\\", "/"),
                "candidates": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
