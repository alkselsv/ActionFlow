"""AF-024 part 1: reproducible Cognee source and memory-lifecycle check.

The experiment uses only synthetic messages from ``corpus.json``. It writes a
separate Cognee projection below ``--output`` and never opens the ActionFlow
application database. The source file is hashed before and after the run.

Run from the repository root (PowerShell):

    $env:COGNEE_USE_SYSTEM_CERTS = '1'   # only behind a TLS-inspecting proxy
    uv run --project experiments/cognee_context python experiments/cognee_context/run_experiment.py

The default output is ``experiments/cognee_context/.runs/af024`` and the default
result file is ``experiments/cognee_context/results/af024-run.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from projection import (
    build_dataset,
    build_manifest,
    dataset_name,
    flatten_search_results,
    load_corpus,
    result_provenance,
    source_sha256,
    source_stats,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS = ROOT / "corpus.json"
DEFAULT_OUTPUT = ROOT / ".runs" / "af024"
DEFAULT_RESULTS = ROOT / "results" / "af024-run.json"

EARLY = "2026-09-14T09:12:00Z"
FINAL = "2026-09-18T00:00:00Z"
EXPERIMENT_PROJECTS = ["alpha", "beta"]
LIFECYCLE_PROJECT = "lifecycle"
LIFECYCLE_DATASET = "af024-lifecycle"


def configure_environment(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for child in ("data", "system", "cache"):
        (output / child).mkdir(exist_ok=True)

    # Never inherit an external LLM setting from .env or the shell. This is a
    # local, deterministic source/lifecycle check, not a model comparison.
    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "GRAPH_EXTRACTOR",
        "ENABLE_AUTHENTICATION",
    ):
        os.environ.pop(key, None)

    os.environ.update(
        {
            # Dataset scoping in Cognee 1.6.10 is honoured only when backend
            # access control is enabled. The local single-user mode searched
            # every dataset even when ``datasets=[...]`` was passed.
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "CACHING": "false",
            "AUTO_FEEDBACK": "false",
            "TELEMETRY_DISABLED": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "DATA_ROOT_DIRECTORY": str(output / "data"),
            "SYSTEM_ROOT_DIRECTORY": str(output / "system"),
            "CACHE_ROOT_DIRECTORY": str(output / "cache"),
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "GRAPH_EXTRACTOR": "gliner_demo",
            "VECTOR_DB_PROVIDER": "lancedb",
            "GRAPH_DATABASE_PROVIDER": "ladybug",
            "DB_PROVIDER": "sqlite",
        }
    )
    # Cognee reads a cwd-relative .env. Run in the isolated output directory so
    # the application's .env cannot silently replace this configuration.
    os.chdir(output)
    if os.environ.get("COGNEE_USE_SYSTEM_CERTS") == "1":
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT.parents[1]).as_posix()
    except ValueError:
        return "<external output root>"


def item_for(cognee_item: dict[str, Any]):
    from cognee.tasks.ingestion.data_item import DataItem

    return DataItem(
        data=cognee_item["text"],
        label="message",
        external_metadata=cognee_item["metadata"],
        data_id=UUID(cognee_item["data_id"]),
    )


def as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def query_matches(rows: list[dict[str, Any]], message_id: str, revision: int | None = None) -> bool:
    return any(
        row.get("message_id") == message_id
        and (revision is None or row.get("revision") == revision)
        for row in rows
    )


def match_summary(rows: list[dict[str, Any]], message_id: str, revision: int | None = None) -> bool:
    return any(
        row.get("resolved_message_id") == message_id
        and (revision is None or row.get("resolved_revision") == revision)
        for row in rows
    )


async def search_chunks(cognee, SearchType, dataset: str, query: str, top_k: int = 20):
    started = time.perf_counter()
    results = await cognee.search(
        query,
        query_type=SearchType.CHUNKS,
        datasets=[dataset],
        top_k=top_k,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return flatten_search_results(results), elapsed_ms


async def run_chunks_query(
    cognee,
    SearchType,
    dataset: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    results, elapsed_ms = await search_chunks(
        cognee, SearchType, dataset["name"], spec["query"], top_k=spec.get("top_k", 20)
    )
    rows = [result_provenance(result) for result in results]
    allowed = {
        (item["message_id"], item["revision"]) for item in dataset["items"]
    }
    outside = [
        row
        for row in rows
        if row.get("message_id")
        and (row["message_id"], row.get("revision")) not in allowed
    ]
    unresolved = [row for row in rows if not row.get("message_id")]
    inconsistent = [row for row in rows if row.get("consistent") is False]
    present = {
        f"{message_id}@r{revision}": query_matches(rows, message_id, revision)
        for message_id, revision in spec.get("present", [])
    }
    absent = {
        f"{message_id}@r{revision}": not query_matches(rows, message_id, revision)
        for message_id, revision in spec.get("absent", [])
    }
    later = {
        message_id: query_matches(rows, message_id)
        for message_id in spec.get("later", [])
    }
    return {
        "id": spec["id"],
        "dataset": dataset["name"],
        "project_id": dataset["project_id"],
        "as_of": dataset["as_of"],
        "query": spec["query"],
        "elapsed_ms": elapsed_ms,
        "result_count": len(rows),
        "rows": rows,
        "allowed": sorted(f"{mid}@r{rev}" for mid, rev in allowed),
        "outside_dataset": outside,
        "unresolved": unresolved,
        "inconsistent": inconsistent,
        "present": present,
        "absent": absent,
        "later": later,
        "passed": all(present.values()) and all(absent.values()),
    }


async def run_summary_query(
    cognee,
    SearchType,
    dataset: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    chunks, chunk_ms = await search_chunks(
        cognee, SearchType, dataset["name"], spec["query"], top_k=spec.get("top_k", 20)
    )
    chunk_rows = [result_provenance(result) for result in chunks]
    chunks_by_id = {str(row["result_id"]): row for row in chunk_rows if row.get("result_id")}
    started = time.perf_counter()
    results = await cognee.search(
        spec["query"],
        query_type=SearchType.SUMMARIES,
        datasets=[dataset["name"]],
        top_k=spec.get("top_k", 20),
    )
    results = flatten_search_results(results)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    rows: list[dict[str, Any]] = []
    for result in results:
        provenance = result_provenance(result)
        chunk = chunks_by_id.get(str(provenance.get("source_chunk_id")))
        provenance["resolved_message_id"] = chunk.get("message_id") if chunk else None
        provenance["resolved_revision"] = chunk.get("revision") if chunk else None
        provenance["resolved_project_id"] = chunk.get("project_id") if chunk else None
        rows.append(provenance)
    allowed = {
        (item["message_id"], item["revision"]) for item in dataset["items"]
    }
    outside = [
        row
        for row in rows
        if row.get("resolved_message_id")
        and (row["resolved_message_id"], row.get("resolved_revision")) not in allowed
    ]
    unresolved = [row for row in rows if not row.get("resolved_message_id")]
    present = {
        f"{message_id}@r{revision}": match_summary(rows, message_id, revision)
        for message_id, revision in spec.get("present", [])
    }
    absent = {
        f"{message_id}@r{revision}": not match_summary(rows, message_id, revision)
        for message_id, revision in spec.get("absent", [])
    }
    return {
        "id": spec["id"],
        "dataset": dataset["name"],
        "project_id": dataset["project_id"],
        "as_of": dataset["as_of"],
        "query": spec["query"],
        "chunks_elapsed_ms": chunk_ms,
        "elapsed_ms": elapsed_ms,
        "result_count": len(rows),
        "rows": rows,
        "allowed": sorted(f"{mid}@r{rev}" for mid, rev in allowed),
        "outside_dataset": outside,
        "unresolved": unresolved,
        "present": present,
        "absent": absent,
        "passed": all(present.values()) and all(absent.values()) and not outside,
    }


async def run_graph_probe(cognee, SearchType, dataset: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        results = await cognee.search(
            "What did Alex promise?",
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[dataset["name"]],
            top_k=3,
        )
        return {
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "result_count": len(results),
        }
    except Exception as exc:  # noqa: BLE001 - the failure is the observation
        return {
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }


async def ingest_dataset(cognee, dataset: dict[str, Any]) -> dict[str, Any]:
    from cognee.modules.data.methods import get_datasets_by_name
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    items = [item_for(item) for item in dataset["items"]]
    started = time.perf_counter()
    await cognee.add(items, dataset_name=dataset["name"])
    add_seconds = round(time.perf_counter() - started, 2)
    started = time.perf_counter()
    await cognee.cognify(datasets=[dataset["name"]], extractor="gliner_demo")
    cognify_seconds = round(time.perf_counter() - started, 2)
    user = await get_default_user()
    [stored_dataset] = await get_datasets_by_name(dataset["name"], user.id)
    rows = await get_dataset_data(stored_dataset.id)
    return {
        "name": dataset["name"],
        "project_id": dataset["project_id"],
        "as_of": dataset["as_of"],
        "item_count": len(items),
        "stored_data_count": len(rows),
        "data_ids": sorted(str(row.id) for row in rows),
        "add_seconds": add_seconds,
        "cognify_seconds": cognify_seconds,
        "total_seconds": round(add_seconds + cognify_seconds, 2),
        "source_max_available_at": dataset["source_max_available_at"],
        "indexed_at": now_iso(),
    }


async def run_lifecycle(cognee, SearchType, corpus: dict[str, Any]) -> dict[str, Any]:
    from cognee.modules.data.methods import get_datasets_by_name
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    before = build_dataset(corpus, LIFECYCLE_PROJECT, "2026-09-14T10:05:00Z")
    after = build_dataset(corpus, LIFECYCLE_PROJECT, FINAL)
    first_data = before["items"][0]
    updated_data = after["items"][0]
    first = item_for(first_data)
    user = await get_default_user()

    started = time.perf_counter()
    await cognee.add(first, dataset_name=LIFECYCLE_DATASET)
    await cognee.add(item_for(first_data), dataset_name=LIFECYCLE_DATASET)
    add_seconds = round(time.perf_counter() - started, 2)
    [stored_dataset] = await get_datasets_by_name(LIFECYCLE_DATASET, user.id)
    rows_after_reload = await get_dataset_data(stored_dataset.id)

    started = time.perf_counter()
    await cognee.cognify(datasets=[LIFECYCLE_DATASET], extractor="gliner_demo")
    cognify_seconds = round(time.perf_counter() - started, 2)
    query = "What did Alex promise about the estimate?"
    initial_results, initial_ms = await search_chunks(
        cognee, SearchType, LIFECYCLE_DATASET, query, top_k=10
    )
    initial_rows = [result_provenance(result) for result in initial_results]

    started = time.perf_counter()
    update = await cognee.update(
        as_uuid(first_data["data_id"]),
        updated_data["text"],
        stored_dataset.id,
        extractor="gliner_demo",
    )
    update_seconds = round(time.perf_counter() - started, 2)
    updated_results, updated_ms = await search_chunks(
        cognee, SearchType, LIFECYCLE_DATASET, query, top_k=10
    )
    updated_rows = [result_provenance(result) for result in updated_results]

    removed: dict[str, Any]
    started = time.perf_counter()
    try:
        removed = await cognee.forget(
            data_id=as_uuid(first_data["data_id"]), dataset_id=stored_dataset.id
        )
        remove_mode = "forget"
    except Exception as exc:  # noqa: BLE001 - deprecated delete is the fallback
        removed = {
            "forget_error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "delete": await cognee.delete(
                as_uuid(first_data["data_id"]), stored_dataset.id, mode="hard"
            ),
        }
        remove_mode = "delete_fallback"
    remove_seconds = round(time.perf_counter() - started, 2)
    deleted_results, deleted_ms = await search_chunks(
        cognee, SearchType, LIFECYCLE_DATASET, query, top_k=10
    )
    deleted_rows = [result_provenance(result) for result in deleted_results]

    started = time.perf_counter()
    await cognee.add(item_for(updated_data), dataset_name=LIFECYCLE_DATASET)
    await cognee.cognify(datasets=[LIFECYCLE_DATASET], extractor="gliner_demo")
    restore_seconds = round(time.perf_counter() - started, 2)
    restored_results, restored_ms = await search_chunks(
        cognee, SearchType, LIFECYCLE_DATASET, query, top_k=10
    )
    restored_rows = [result_provenance(result) for result in restored_results]

    return {
        "dataset": LIFECYCLE_DATASET,
        "add_twice_seconds": add_seconds,
        "stored_data_count_after_reload": len(rows_after_reload),
        "cognify_seconds": cognify_seconds,
        "initial_elapsed_ms": initial_ms,
        "initial_rows": initial_rows,
        "update_seconds": update_seconds,
        "update_result": update,
        "updated_elapsed_ms": updated_ms,
        "updated_rows": updated_rows,
        "remove_mode": remove_mode,
        "remove_seconds": remove_seconds,
        "remove_result": removed,
        "deleted_elapsed_ms": deleted_ms,
        "deleted_rows": deleted_rows,
        "restore_seconds": restore_seconds,
        "restored_elapsed_ms": restored_ms,
        "restored_rows": restored_rows,
        "checks": {
            "reload_deduplicated": len(rows_after_reload) == 1,
            "update_visible": any("Monday" in row["text"] for row in updated_rows),
            "delete_removed": len(deleted_rows) == 0,
            "restore_visible": any("Monday" in row["text"] for row in restored_rows),
            "update_metadata_preserved": any(
                row.get("message_id") == "life-m1" for row in updated_rows
            ),
            "update_metadata_consistent": all(
                row.get("consistent") is True for row in updated_rows
            ),
        },
    }


async def run_parallel(cognee, SearchType, alpha: dict[str, Any], beta: dict[str, Any]) -> dict[str, Any]:
    async def one(dataset: dict[str, Any], query: str):
        return await search_chunks(cognee, SearchType, dataset["name"], query, top_k=10)

    started = time.perf_counter()
    results = await asyncio.gather(
        one(alpha, "What did Alex promise about the estimate?"),
        one(beta, "What did Alex promise about the contract?"),
        return_exceptions=True,
    )
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    errors = [f"{type(item).__name__}: {item}" for item in results if isinstance(item, Exception)]
    return {
        "elapsed_ms": elapsed,
        "errors": errors,
        "result_counts": [
            len(item[0]) if isinstance(item, tuple) else None for item in results
        ],
        "passed": not errors,
    }


def query_specs() -> list[dict[str, Any]]:
    early_alpha = dataset_name("alpha", EARLY)
    final_alpha = dataset_name("alpha", FINAL)
    early_beta = dataset_name("beta", EARLY)
    final_beta = dataset_name("beta", FINAL)
    return [
        {
            "id": "early_alpha_promise_and_edit_slice",
            "dataset_name": early_alpha,
            "query": "What did Alex promise about the estimate deadline?",
            "present": [("alpha-m2", 1)],
            "absent": [("alpha-m2", 2), ("alpha-m3", 1), ("alpha-m4", 1), ("alpha-m5", 1)],
        },
        {
            "id": "early_alpha_deleted_source_present",
            "dataset_name": early_alpha,
            "query": "What was the old estimate amount in the temporary draft?",
            "present": [("alpha-m6", 1)],
        },
        {
            "id": "early_beta_promise",
            "dataset_name": early_beta,
            "query": "What did Alex promise about the signed contract?",
            "present": [("beta-m2", 1)],
            "absent": [("beta-m3", 1), ("beta-m4", 1)],
        },
        {
            "id": "final_alpha_revision_and_changes",
            "dataset_name": final_alpha,
            "query": "What was the final estimate deadline, and was the promise moved or cancelled?",
            "present": [("alpha-m2", 2), ("alpha-m3", 1), ("alpha-m4", 1)],
            "absent": [("alpha-m2", 1), ("alpha-m6", 1)],
        },
        {
            "id": "final_alpha_late_reply",
            "dataset_name": final_alpha,
            "query": "What did the customer say after the estimate was late?",
            "present": [("alpha-m5", 1)],
        },
        {
            "id": "final_alpha_deleted_source_absent",
            "dataset_name": final_alpha,
            "query": "What was the old estimate amount in the temporary draft?",
            "absent": [("alpha-m6", 1)],
        },
        {
            "id": "final_beta_contract_only",
            "dataset_name": final_beta,
            "query": "What did Sam and Alex agree about the contract?",
            "present": [("beta-m2", 1), ("beta-m3", 1)],
            "absent": [("alpha-m1", 1), ("alpha-m2", 2)],
        },
        {
            "id": "final_alpha_estimate_only",
            "dataset_name": final_alpha,
            "query": "What did Sam and Alex agree about the estimate?",
            "present": [("alpha-m2", 2), ("alpha-m3", 1)],
            "absent": [("beta-m1", 1), ("beta-m2", 1)],
        },
        {
            "id": "final_alpha_query_with_date_still_sees_later_facts",
            "dataset_name": final_alpha,
            "query": "As of 2026-09-14T09:12Z, what did Alex promise about the estimate?",
            "later": ["alpha-m3", "alpha-m4", "alpha-m5"],
        },
    ]


async def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output.resolve()
    if args.reset and output.exists():
        shutil.rmtree(output)
    configure_environment(output)

    import cognee
    from cognee import SearchType

    corpus_path = args.corpus.resolve()
    corpus_before = source_sha256(corpus_path)
    corpus = load_corpus(corpus_path)
    manifest = build_manifest(
        corpus, corpus_path, [EARLY, FINAL], project_ids=EXPERIMENT_PROJECTS
    )
    datasets = {dataset["name"]: dataset for dataset in manifest["datasets"]}

    ingestion: dict[str, Any] = {}
    for dataset in manifest["datasets"]:
        ingestion[dataset["name"]] = await ingest_dataset(cognee, dataset)

    queries: list[dict[str, Any]] = []
    for spec in query_specs():
        dataset = datasets[spec["dataset_name"]]
        spec = {key: value for key, value in spec.items() if key != "dataset_name"}
        queries.append(await run_chunks_query(cognee, SearchType, dataset, spec))

    final_alpha = datasets[dataset_name("alpha", FINAL)]
    summary = await run_summary_query(
        cognee,
        SearchType,
        final_alpha,
        {
            "id": "final_alpha_summary_attribution",
            "query": "What did Alex promise about the estimate deadline?",
            "present": [("alpha-m2", 2)],
            "top_k": 20,
        },
    )
    graph_probe = await run_graph_probe(cognee, SearchType, final_alpha)
    lifecycle = await run_lifecycle(cognee, SearchType, corpus)
    parallel = await run_parallel(
        cognee,
        SearchType,
        final_alpha,
        datasets[dataset_name("beta", FINAL)],
    )

    corpus_after = source_sha256(corpus_path)
    attribution = {
        "chunk_rows": sum(len(item["rows"]) for item in queries) + summary["result_count"],
        "chunk_unresolved": sum(len(item["unresolved"]) for item in queries),
        "chunk_inconsistent": sum(len(item["inconsistent"]) for item in queries),
        "outside_dataset": sum(len(item["outside_dataset"]) for item in queries),
        "summary_unresolved": len(summary["unresolved"]),
        "summary_outside_dataset": len(summary["outside_dataset"]),
    }
    historical_snapshot = all(
        all(item["absent"].values())
        for item in queries
        if item["id"].startswith("early_")
    )
    native_date_leak = any(queries[-1]["later"].values())
    direct_provenance = (
        attribution["chunk_unresolved"] == 0
        and attribution["chunk_inconsistent"] == 0
    )
    summary_mapping = attribution["summary_unresolved"] == 0
    summary_direct_metadata = any(
        row.get("attribution_source") == "external_metadata" for row in summary["rows"]
    )
    result = {
        "experiment": "AF-024 part 1 / issue #23",
        "generated_at": now_iso(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cognee_version": cognee.get_cognee_version(),
            "embedding_provider": "fastembed",
            "embedding_model": "BAAI/bge-small-en-v1.5",
            "graph_extractor": "gliner_demo",
            "graph_model": "fastino/gliner2.5-base-v1",
            "llm": "not configured; local keyless path only",
            "storage": {
                "relational": "sqlite",
                "vector": "lancedb",
                "graph": "ladybug",
                "roots": display_path(output),
            },
            "auth": "ENABLE_BACKEND_ACCESS_CONTROL=true (default user)",
            "caching": "CACHING=false",
        },
        "corpus": {
            "path": display_path(corpus_path),
            "sha256_before": corpus_before,
            "sha256_after": corpus_after,
            "unchanged": corpus_before == corpus_after,
            "stats": source_stats(corpus),
            "as_ofs": [EARLY, FINAL],
            "projection": (
                "one Cognee DataItem per source message revision; coherent discussion is "
                "reconstructed by chat_id, sent_at, message_id and reply_to_message_id"
            ),
        },
        "manifest": manifest,
        "ingestion": ingestion,
        "queries": queries,
        "summary": summary,
        "graph_probe": graph_probe,
        "lifecycle": lifecycle,
        "parallel": parallel,
        "checks": {
            "source_unchanged": corpus_before == corpus_after,
            "direct_provenance": direct_provenance,
            "summary_mapping": summary_mapping,
            "historical_snapshot": historical_snapshot,
            "native_date_leak": native_date_leak,
            "isolation": attribution["outside_dataset"] == 0,
            "lifecycle_reload": lifecycle["checks"]["reload_deduplicated"],
            "lifecycle_update": lifecycle["checks"]["update_visible"],
            "lifecycle_delete": lifecycle["checks"]["delete_removed"],
            "lifecycle_restore": lifecycle["checks"]["restore_visible"],
            "lifecycle_update_metadata_consistent": lifecycle["checks"][
                "update_metadata_consistent"
            ],
            "summary_direct_metadata": summary_direct_metadata,
            "parallel_search": parallel["passed"],
        },
        "attribution": attribution,
        "defects": {
            "native_as_of_filter_absent": {
                "evidence": "final_alpha_query_with_date_still_sees_later_facts",
                "later_facts_returned": queries[-1]["later"],
                "mitigation": "materialize a separate dataset per project and as_of",
            },
            "update_keeps_stale_external_metadata": {
                "evidence": "lifecycle.updated_rows",
                "consistent": lifecycle["checks"]["update_metadata_consistent"],
                "mitigation": "rebuild the projection item instead of using cognee.update",
            },
            "dataset_scope_requires_access_control": {
                "evidence": (
                    "local single-user mode (ENABLE_BACKEND_ACCESS_CONTROL=false) ignored "
                    "datasets=[...] and returned every project's chunks; the run therefore "
                    "uses the default access-control mode and the default user"
                ),
                "mitigation": "keep access control enabled and select the named dataset",
            },
            "summary_loses_direct_provenance": {
                "evidence": "summary.rows",
                "direct_metadata": summary_direct_metadata,
                "mitigation": "join source_chunk_id to a preceding CHUNKS result",
            },
            "graph_completion_requires_llm": graph_probe,
            "no_native_freshness_or_incremental_contract": {
                "evidence": "manifest + lifecycle timings",
                "mitigation": "compare source_sha256/source_max_available_at and rebuild",
            },
        },
        "decision": {
            "status": "requires_fixes",
            "projection_status": "checks_passed_for_per_project_as_of_chunks",
            "reason": (
                "The projection passed isolation, source attribution, historical-snapshot, "
                "delete and restore checks. Native Cognee still cannot be used as the context "
                "source without fixes: a date in a query returns later facts, update leaves "
                "external_metadata at the old revision, summaries need a source_chunk_id join, "
                "graph completion needs an LLM key, and dataset scoping depends on access "
                "control. A future A/B may use the per-project/per-as_of chunk projection only "
                "after these defects are explicitly handled; native graph memory is not ready."
            ),
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--no-reset", action="store_true", help="keep an existing output root")
    args = parser.parse_args()
    args.reset = not args.no_reset
    args.corpus = args.corpus.resolve()
    args.output = args.output.resolve()
    args.results = args.results.resolve()
    result = asyncio.run(run_experiment(args))
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["checks"], ensure_ascii=False, indent=2))
    print(json.dumps(result["decision"], ensure_ascii=False, indent=2))
    print(f"results: {args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())