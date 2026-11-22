import json
from pathlib import Path

from projection import (
    build_dataset,
    build_manifest,
    discussion,
    excluded_messages,
    first_line_fields,
    flatten_search_results,
    load_corpus,
    make_item,
    result_provenance,
    source_sha256,
    source_stats,
    visible_messages,
)

CORPUS_PATH = Path(__file__).resolve().parents[1] / "corpus.json"
EARLY = "2026-09-14T09:12:00Z"
FINAL = "2026-09-18T00:00:00Z"


def corpus():
    return load_corpus(CORPUS_PATH)


def ids(messages):
    return [(message["message_id"], message["revision"]) for message in messages]


def test_early_slice_has_revision_one_and_no_later_facts():
    selected = visible_messages(corpus(), "alpha", EARLY)
    assert ids(selected) == [
        ("alpha-m1", 1),
        ("alpha-m2", 1),
        ("alpha-m6", 1),
    ]
    excluded = excluded_messages(corpus(), "alpha", EARLY)
    assert {
        (item["message_id"], item["revision"], item["reason"]) for item in excluded
    } >= {
        ("alpha-m2", "2", "not_available"),
        ("alpha-m3", "1", "sent_after_as_of"),
        ("alpha-m4", "1", "sent_after_as_of"),
        ("alpha-m5", "1", "sent_after_as_of"),
    }


def test_final_slice_uses_latest_revision_and_omits_deleted_source():
    selected = visible_messages(corpus(), "alpha", FINAL)
    assert ids(selected) == [
        ("alpha-m1", 1),
        ("alpha-m2", 2),
        ("alpha-m3", 1),
        ("alpha-m4", 1),
        ("alpha-m5", 1),
    ]
    assert ("alpha-m6", 1) not in ids(selected)
    excluded = excluded_messages(corpus(), "alpha", FINAL)
    assert ("alpha-m2", "1", "superseded_revision") in {
        (item["message_id"], item["revision"], item["reason"]) for item in excluded
    }
    assert ("alpha-m6", "1", "deleted_source") in {
        (item["message_id"], item["revision"], item["reason"]) for item in excluded
    }


def test_same_display_names_are_isolated_by_project():
    alpha = build_dataset(corpus(), "alpha", FINAL)
    beta = build_dataset(corpus(), "beta", FINAL)
    assert all(item["metadata"]["project"] == "alpha" for item in alpha["items"])
    assert all(item["metadata"]["project"] == "beta" for item in beta["items"])
    assert {item["message_id"] for item in alpha["items"]} == {
        "alpha-m1",
        "alpha-m2",
        "alpha-m3",
        "alpha-m4",
        "alpha-m5",
    }
    assert {item["message_id"] for item in beta["items"]} == {
        "beta-m1",
        "beta-m2",
        "beta-m3",
        "beta-m4",
    }


def test_discussion_is_reconstructed_in_time_order_with_reply_links():
    messages = discussion(corpus(), "alpha", FINAL)
    assert [item["sent_at"] for item in messages] == sorted(
        item["sent_at"] for item in messages
    )
    assert messages[1]["reply_to_message_id"] == "alpha-m1"
    assert messages[2]["reply_to_message_id"] == "alpha-m2"


def test_item_contains_stable_source_identity():
    item = make_item(corpus(), "alpha", visible_messages(corpus(), "alpha", FINAL)[1])
    second = make_item(corpus(), "alpha", visible_messages(corpus(), "alpha", FINAL)[1])
    header = first_line_fields(item["text"])
    assert item["data_id"] == second["data_id"]
    assert header["message_id"] == "alpha-m2"
    assert header["revision"] == "2"
    assert header["project"] == "alpha"
    assert header["author_id"] == "emp-alpha-alex"
    metadata = json.loads(json.dumps(item["metadata"]))
    assert metadata["message_id"] == "alpha-m2"
    assert metadata["revision"] == 2
    assert metadata["data_id"] == item["data_id"]


def test_result_provenance_prefers_metadata_and_detects_mismatch():
    item = make_item(corpus(), "alpha", visible_messages(corpus(), "alpha", FINAL)[1])
    result = {
        "id": "chunk-1",
        "text": item["text"],
        "external_metadata": json.dumps(item["metadata"]),
    }
    provenance = result_provenance(result)
    assert provenance["message_id"] == "alpha-m2"
    assert provenance["revision"] == 2
    assert provenance["attribution_source"] == "external_metadata"
    assert provenance["consistent"] is True

    mismatch = {**result, "text": item["text"].replace("message_id=alpha-m2", "message_id=beta-m2")}
    assert result_provenance(mismatch)["consistent"] is False


def test_acl_payload_is_unwrapped_without_losing_source_metadata():
    item = make_item(corpus(), "alpha", visible_messages(corpus(), "alpha", FINAL)[1])
    chunk = {
        "id": "chunk-1",
        "text": item["text"],
        "external_metadata": json.dumps(item["metadata"]),
    }
    grouped = {
        "dataset_id": "dataset-1",
        "dataset_name": "af024-alpha-final",
        "search_result": [chunk],
    }
    [flattened] = flatten_search_results([grouped])
    provenance = result_provenance(flattened)
    assert provenance["message_id"] == "alpha-m2"
    assert provenance["revision"] == 2
    assert provenance["attribution_source"] == "external_metadata"


def test_manifest_has_unique_items_and_restore_metadata():
    manifest = build_manifest(
        corpus(), CORPUS_PATH, [EARLY, FINAL], project_ids=["alpha", "beta"]
    )
    assert manifest["source_sha256"] == source_sha256(CORPUS_PATH)
    assert len(manifest["datasets"]) == 4
    all_data_ids = []
    for dataset in manifest["datasets"]:
        data_ids = [item["data_id"] for item in dataset["items"]]
        all_data_ids.extend(data_ids)
        assert len(data_ids) == len(set(data_ids))
        assert dataset["discussion_ids"] == [
            {"message_id": item["message_id"], "revision": item["revision"]}
            for item in dataset["items"]
        ]
        assert dataset["source_max_available_at"] is not None
    assert len(all_data_ids) == len(set(all_data_ids))


def test_projection_does_not_change_corpus_bytes():
    before = source_sha256(CORPUS_PATH)
    data = corpus()
    for project_id in ("alpha", "beta", "lifecycle"):
        visible_messages(data, project_id, FINAL)
        build_dataset(data, project_id, FINAL)
    assert source_sha256(CORPUS_PATH) == before


def test_source_stats_include_lifecycle_fixture():
    stats = source_stats(corpus())
    assert stats["project_count"] == 3
    assert stats["message_count"] == 11
    assert stats["revision_count"] == 13
