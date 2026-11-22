from decimal import Decimal

FINDING_TYPES = ("unanswered", "commitment", "complaint")


def record_detector_result(run, detector, result, duration_ms, summary):
    results = dict((run.limitations or {}).get("detectors", {}))
    results[result.finding_type] = {
        **summary,
        "provider": detector.provider,
        "model": detector.model_id,
        "detector_version": detector.detector_version,
        "prompt_version": detector.prompt_version,
        "schema_version": detector.schema_version,
        "parameters": detector.model_params,
        "duration_ms": duration_ms,
        "usage": result.usage.model_dump(mode="json"),
        "diagnostics": result.diagnostics,
    }
    run.limitations = {**(run.limitations or {}), "detectors": results}
    costs = [item["usage"]["cost_usd"] for item in results.values()]
    run.cost_usd = (
        sum((Decimal(value) for value in costs), Decimal(0))
        if all(value is not None for value in costs)
        else None
    )
    run.detector_duration_ms = sum(item["duration_ms"] for item in results.values())
    run.candidate_count = sum(item["candidate_count"] for item in results.values())
    run.invalid_candidate_count = sum(
        item["invalid_candidate_count"] for item in results.values()
    )


def detector_results(run):
    limitations = run.limitations or {}
    saved = limitations.get("detectors", {})
    completed = {kind for kind in FINDING_TYPES if kind in saved or kind in limitations}
    failed = next((kind for kind in FINDING_TYPES if kind not in completed), None)
    return {
        kind: {
            **(saved.get(kind) or limitations.get(kind) or {}),
            "status": (
                "succeeded"
                if kind in completed
                else "failed"
                if run.status == "failed" and run.stage == "detector" and kind == failed
                else "not_run"
                if run.status in {"succeeded", "failed"}
                else "pending"
            ),
        }
        for kind in FINDING_TYPES
    }
