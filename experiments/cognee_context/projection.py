"""Pure projection helpers for the AF-024 Cognee source/lifecycle experiment.

Cognee is only used by ``run_experiment.py``. This module deliberately depends
on the standard library so the source-selection and provenance rules can be
tested without downloading Cognee, torch, GLiNER, or embedding models.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MESSAGE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
HEADER_PREFIX = "[actionflow]"


def utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(ISO_FORMAT)


def parse_as_of(value: str) -> datetime:
    parsed = utc(value)
    if parsed is None:
        raise ValueError("as_of is required")
    return parsed


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_corpus(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synthetic") is not True:
        raise ValueError("AF-024 only accepts an explicitly synthetic corpus")
    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("corpus must contain at least one project")
    project_ids: set[str] = set()
    message_ids: set[tuple[str, str, int]] = set()
    for project in projects:
        project_id = project.get("project_id")
        if not isinstance(project_id, str) or not MESSAGE_ID.fullmatch(project_id):
            raise ValueError(f"invalid project_id: {project_id!r}")
        if project_id in project_ids:
            raise ValueError(f"duplicate project_id: {project_id}")
        project_ids.add(project_id)
        messages = project.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"project {project_id} has no messages")
        for message in messages:
            message_id = message.get("message_id")
            revision = message.get("revision")
            if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
                raise ValueError(f"invalid message_id: {message_id!r}")
            if not isinstance(revision, int) or revision < 1:
                raise ValueError(f"invalid revision for {message_id}: {revision!r}")
            key = (project_id, message_id, revision)
            if key in message_ids:
                raise ValueError(f"duplicate revision: {key}")
            message_ids.add(key)
            for field in ("author_id", "author_name", "side", "text", "sent_at", "available_at"):
                if not isinstance(message.get(field), str) or not message[field]:
                    raise ValueError(f"{message_id} revision {revision} has no {field}")
            for field in ("edited_at", "deleted_at"):
                if message.get(field) is not None:
                    utc(message[field])
            utc(message["sent_at"])
            utc(message["available_at"])
    return data


def project(corpus: dict[str, Any], project_id: str) -> dict[str, Any]:
    for item in corpus["projects"]:
        if item["project_id"] == project_id:
            return item
    raise KeyError(project_id)


def visible_messages(
    corpus: dict[str, Any], project_id: str, as_of: str | datetime
) -> list[dict[str, Any]]:
    """Select the revision of each message available at ``as_of``.

    This mirrors the useful part of the application's historical input rule:
    a message is visible only when its text was sent, observed, and edited by
    the cutoff, and it has not been deleted by that cutoff. The source corpus
    itself is never modified; a deleted source remains in the canonical file
    but is absent from a later projection.
    """

    cutoff = parse_as_of(as_of) if isinstance(as_of, str) else utc(as_of)
    assert cutoff is not None
    selected: dict[str, dict[str, Any]] = {}
    for message in project(corpus, project_id)["messages"]:
        sent_at = utc(message["sent_at"])
        available_at = utc(message["available_at"])
        edited_at = utc(message.get("edited_at"))
        deleted_at = utc(message.get("deleted_at"))
        if sent_at is None or sent_at > cutoff:
            continue
        if available_at is None or available_at > cutoff:
            continue
        if edited_at is not None and edited_at > cutoff:
            continue
        if deleted_at is not None and deleted_at <= cutoff:
            continue
        current = selected.get(message["message_id"])
        candidate_key = (
            int(message["revision"]),
            edited_at or sent_at,
            available_at,
        )
        if current is None:
            selected[message["message_id"]] = message
            continue
        current_key = (
            int(current["revision"]),
            utc(current.get("edited_at")) or utc(current["sent_at"]),
            utc(current["available_at"]),
        )
        if candidate_key > current_key:
            selected[message["message_id"]] = message
    return sorted(
        selected.values(),
        key=lambda item: (utc(item["sent_at"]), item["message_id"]),
    )


def excluded_messages(
    corpus: dict[str, Any], project_id: str, as_of: str | datetime
) -> list[dict[str, str]]:
    cutoff = parse_as_of(as_of) if isinstance(as_of, str) else utc(as_of)
    assert cutoff is not None
    selected_keys = {
        (item["message_id"], int(item["revision"]))
        for item in visible_messages(corpus, project_id, cutoff)
    }
    excluded: list[dict[str, str]] = []
    for message in project(corpus, project_id)["messages"]:
        key = (message["message_id"], int(message["revision"]))
        if key in selected_keys:
            continue
        if utc(message["sent_at"]) > cutoff:
            reason = "sent_after_as_of"
        elif utc(message["available_at"]) > cutoff:
            reason = "not_available"
        elif message.get("edited_at") is not None and utc(message["edited_at"]) > cutoff:
            reason = "future_edit"
        elif message.get("deleted_at") is not None and utc(message["deleted_at"]) <= cutoff:
            reason = "deleted_source"
        else:
            reason = "superseded_revision"
        excluded.append(
            {
                "message_id": message["message_id"],
                "revision": str(message["revision"]),
                "reason": reason,
            }
        )
    return excluded


def dataset_name(project_id: str, as_of: str | datetime) -> str:
    cutoff = parse_as_of(as_of) if isinstance(as_of, str) else utc(as_of)
    assert cutoff is not None
    return f"af024-{project_id}-{cutoff.strftime('%Y%m%dT%H%M%SZ')}"


def data_id(
    project_id: str, message_id: str, revision: int, scope: str | None = None
) -> str:
    prefix = scope or project_id
    return str(
        uuid5(
            NAMESPACE_URL,
            f"actionflow/cognee/{prefix}/{message_id}/r{revision}",
        )
    )


def _header_value(value: Any) -> str:
    if value is None:
        return "-"
    return str(value).replace("\n", " ").strip()


def make_item(
    corpus: dict[str, Any],
    project_id: str,
    message: dict[str, Any],
    scope: str | None = None,
) -> dict[str, Any]:
    header = {
        "project": project_id,
        "chat_id": project(corpus, project_id)["chat_id"],
        "message_id": message["message_id"],
        "revision": message["revision"],
        "author_id": message["author_id"],
        "author": message["author_name"],
        "side": message["side"],
        "sent_at": message["sent_at"],
        "available_at": message["available_at"],
        "edited_at": message.get("edited_at"),
        "deleted_at": message.get("deleted_at"),
        "reply_to_message_id": message.get("reply_to_message_id"),
    }
    header_line = HEADER_PREFIX + " " + " ".join(
        f"{key}={_header_value(value)}" for key, value in header.items()
    )
    metadata = {
        **header,
        "message_id": message["message_id"],
        "revision": int(message["revision"]),
        "data_id": data_id(
            project_id, message["message_id"], int(message["revision"]), scope
        ),
    }
    return {
        "data_id": metadata["data_id"],
        "text": header_line + "\n" + message["text"],
        "metadata": metadata,
        "message_id": message["message_id"],
        "revision": int(message["revision"]),
    }


def build_dataset(
    corpus: dict[str, Any], project_id: str, as_of: str | datetime
) -> dict[str, Any]:
    selected = visible_messages(corpus, project_id, as_of)
    cutoff = parse_as_of(as_of) if isinstance(as_of, str) else utc(as_of)
    assert cutoff is not None
    name = dataset_name(project_id, cutoff)
    items = [make_item(corpus, project_id, message, scope=name) for message in selected]
    if len({item["data_id"] for item in items}) != len(items):
        raise ValueError(f"duplicate projection data_id in {project_id}")
    source_max = max((utc(item["available_at"]) for item in selected), default=None)
    return {
        "name": name,
        "project_id": project_id,
        "as_of": iso(cutoff),
        "discussion_ids": [
            {"message_id": item["message_id"], "revision": item["revision"]}
            for item in items
        ],
        "items": items,
        "excluded": excluded_messages(corpus, project_id, cutoff),
        "source_max_available_at": iso(source_max) if source_max else None,
    }


def build_manifest(
    corpus: dict[str, Any],
    corpus_path: Path,
    as_ofs: list[str],
    project_ids: list[str] | None = None,
) -> dict[str, Any]:
    datasets: list[dict[str, Any]] = []
    selected_projects = project_ids or [item["project_id"] for item in corpus["projects"]]
    for as_of in as_ofs:
        for project_id in selected_projects:
            datasets.append(build_dataset(corpus, project_id, as_of))
    return {
        "source_sha256": source_sha256(corpus_path),
        "as_ofs": [iso(parse_as_of(value)) for value in as_ofs],
        "datasets": datasets,
    }


def discussion(corpus: dict[str, Any], project_id: str, as_of: str | datetime) -> list[dict[str, Any]]:
    """A coherent discussion is reconstructed by stable message IDs and time.

    The experiment projects one document per message, not one mutable document
    per conversation. This keeps every retrieved line attributable to a source
    message and avoids rewriting a whole thread when one message changes. The
    discussion order and reply links are reconstructed from the source fields.
    """

    return visible_messages(corpus, project_id, as_of)


def first_line_fields(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    line = text.splitlines()[0]
    if not line.startswith(HEADER_PREFIX):
        return {}
    fields: dict[str, str] = {}
    for part in line[len(HEADER_PREFIX) :].strip().split():
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    return fields


def flatten_search_results(results: list[Any]) -> list[Any]:
    """Unwrap the per-dataset payloads that Cognee returns in ACL mode.

    With access control enabled, ``search`` returns one object per dataset with
    ``search_result`` (or ``text_result`` in verbose mode) inside. With it
    disabled the results are flat. The experiment accepts both shapes so the
    same provenance code is exercised in either configuration.
    """

    flattened: list[Any] = []
    for result in results:
        raw = _result_dict(result)
        nested = raw.get("search_result")
        if nested is None:
            nested = raw.get("text_result")
        if isinstance(nested, list):
            flattened.extend(nested)
        else:
            flattened.append(result)
    return flattened


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    values = getattr(result, "__dict__", None)
    return values if isinstance(values, dict) else {}


def result_provenance(result: Any) -> dict[str, Any]:
    """Resolve a Cognee result to a source message without trusting the file.

    ``external_metadata`` is the primary attribution. The text header is
    checked as a second signal. A result is not accepted as exact attribution
    merely because it came from a dataset built from a source file.
    """

    raw = _result_dict(result)
    text = raw.get("text")
    external = raw.get("external_metadata")
    metadata: dict[str, Any] = {}
    if isinstance(external, str) and external:
        try:
            parsed = json.loads(external)
            if isinstance(parsed, dict):
                metadata = parsed
        except json.JSONDecodeError:
            metadata = {}
    elif isinstance(external, dict):
        metadata = external
    header = first_line_fields(text)
    metadata_id = metadata.get("message_id")
    header_id = header.get("message_id")
    metadata_revision = metadata.get("revision")
    header_revision = header.get("revision")
    if metadata_revision is not None:
        try:
            metadata_revision = int(metadata_revision)
        except (TypeError, ValueError):
            metadata_revision = None
    if header_revision is not None:
        try:
            header_revision = int(header_revision)
        except (TypeError, ValueError):
            header_revision = None
    resolved_id = metadata_id or header_id
    resolved_revision = metadata_revision if metadata_id else header_revision
    consistent = True
    if metadata_id and header_id and metadata_id != header_id:
        consistent = False
    if (
        metadata_revision is not None
        and header_revision is not None
        and metadata_revision != header_revision
    ):
        consistent = False
    return {
        "result_id": raw.get("id"),
        "source_chunk_id": raw.get("source_chunk_id"),
        "message_id": resolved_id,
        "revision": resolved_revision,
        "project_id": metadata.get("project") or header.get("project"),
        "author_id": metadata.get("author_id") or header.get("author_id"),
        "sent_at": metadata.get("sent_at") or header.get("sent_at"),
        "attribution_source": (
            "external_metadata"
            if metadata_id
            else "text_header"
            if header_id
            else "none"
        ),
        "consistent": consistent,
        "text": (text or "")[:2000],
    }


def chunk_map(chunk_results: list[Any]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for result in chunk_results:
        raw = _result_dict(result)
        result_id = raw.get("id")
        if result_id:
            mapped[str(result_id)] = result_provenance(result)
    return mapped


def source_stats(corpus: dict[str, Any]) -> dict[str, int]:
    project_count = len(corpus["projects"])
    message_ids = {
        (project_data["project_id"], message["message_id"])
        for project_data in corpus["projects"]
        for message in project_data["messages"]
    }
    revisions = sum(len(project_data["messages"]) for project_data in corpus["projects"])
    return {
        "project_count": project_count,
        "message_count": len(message_ids),
        "revision_count": revisions,
    }