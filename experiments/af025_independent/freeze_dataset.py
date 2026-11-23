"""Validate AF-025 v2 floor and freeze inputs before evaluation."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = Path(__file__).resolve().parent / "v2"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    annotation_path = TASK / "annotation.json"
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    chats = annotation["cases"]
    assert len(chats) >= 10
    assert sum(item["message_count"] for item in chats) >= 30
    assert min(Counter(item["expected"]["type"] for item in chats).values()) >= 5
    assert len({item["chat"] for item in chats}) == len(chats)
    fixture_ids = {p.stem.removeprefix("north-conv-") for p in (TASK / "fixtures").glob("*.json")}
    assert fixture_ids == {item["chat"] for item in chats}
    dev_ids = {f"n-{n:03d}" for n in range(1, 9)} | {"s-001"}
    assert not fixture_ids & dev_ids
    cutoff = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    for fixture in sorted((TASK / "fixtures").glob("*.json")):
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        assert payload["_actionflow_fixture"]["synthetic"] is True
        assert payload["_actionflow_fixture"]["not_a_real_telegram_export"] is True
        for message in payload["messages"]:
            source = message["_actionflow_synthetic_source"]
            assert source["revision"] == 1
            assert source["sender_role"] in {"customer", "employee"}
            assert datetime.fromisoformat(source["sent_at"]).astimezone(timezone.utc) <= cutoff
    hashes = {
        p.relative_to(ROOT).as_posix(): sha(p)
        for p in [annotation_path, *sorted((TASK / "fixtures").glob("*.json"))]
    }
    (TASK / "frozen_manifest.json").write_text(json.dumps({
        "dataset_id": annotation["dataset_id"],
        "frozen_before_run": True,
        "chats": len(chats),
        "messages": sum(item["message_count"] for item in chats),
        "expected_by_type": dict(Counter(item["expected"]["type"] for item in chats)),
        "development_overlap": [],
        "input_sha256": hashes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("OK: frozen", len(chats), "chats /", sum(item["message_count"] for item in chats), "messages")


if __name__ == "__main__":
    main()
