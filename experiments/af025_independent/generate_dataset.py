"""Create a frozen, agent-authored synthetic AF-025 evaluation corpus.

The corpus is deliberately disjoint from AF-002 chat IDs. It is a diagnostic
synthetic evaluation, not evidence of real-customer performance.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "v2"
FIXTURES = ROOT / "fixtures"
CASES = [
    ("unanswered", [
        ("customer", "Добрый день, посмотрели спецификацию?") ,
        ("customer", "Почему в смете дважды учтена настройка выгрузки?"),
        ("employee", "Проверю расчёт и вернусь с пояснением."),
    ]),
    ("commitment", [
        ("customer", "Когда будет готова сводка по этапу?"),
        ("employee", "Исправленную смету пришлю завтра до 12:00."),
        ("customer", "Принято, ожидаю."),
    ]),
    ("complaint", [
        ("customer", "Третий раз интеграция сбрасывает настройки, это не устраивает."),
        ("employee", "Понимаю, уточню ситуацию."),
        ("customer", "Прошу зафиксировать претензию и дать план исправления."),
    ]),
]


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset_id": "af025-synthetic-independent-v2",
        "synthetic": True,
        "authorship": "agent-generated before evaluation; not independently human-annotated",
        "development_overlap": [],
        "protocol": "docs/experiment/af-025-evaluation-protocol.md",
        "cases": [],
    }
    for idx in range(15):
        finding_type, messages = CASES[idx % 3]
        case_no = idx + 1
        code = f"n-af25-{case_no:03d}"
        base_time = datetime(2026, 9, 20, 9, 0) + timedelta(minutes=idx * 25)
        records = []
        for message_no, (role, text) in enumerate(messages, start=1):
            sent = base_time + timedelta(minutes=message_no * 5)
            author_id = "customer-1" if role == "customer" else "employee-1"
            records.append({
                "id": message_no,
                "type": "message",
                "date": sent.isoformat(),
                "date_unixtime": str(int(sent.timestamp())),
                "from": "Synthetic Customer" if role == "customer" else "Synthetic Employee",
                "from_id": author_id,
                "text": text,
                "_actionflow_synthetic_source": {
                    "external_id": f"{code}-{message_no:03d}",
                    "revision": 1,
                    "sender_role": role,
                    "sent_at": sent.isoformat() + "+03:00",
                    "observed_at": (sent + timedelta(minutes=1)).isoformat() + "+03:00",
                    "reply_to_external_id": None if message_no == 1 else f"{code}-{message_no-1:03d}",
                    "source": "agent_generated_synthetic",
                },
            })
        payload = {
            "name": f"Synthetic AF-025 {case_no:03d}",
            "type": "personal_chat",
            "id": 25000 + case_no,
            "messages": records,
            "_actionflow_fixture": {
                "synthetic": True,
                "workspace_id": "af25-independent",
                "conversation_id": code,
                "source_record_count": len(records),
                "source_export_format": "agent-generated synthetic",
                "not_a_real_telegram_export": True,
            },
        }
        (FIXTURES / f"north-conv-{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        expected = {"type": finding_type, "anchor": 2 if finding_type != "complaint" else 1,
                    "evidence": [2, 3] if finding_type == "unanswered" else ([2] if finding_type == "commitment" else [1, 2, 3]),
                    "critical": finding_type == "complaint"}
        manifest["cases"].append({"chat": code, "expected": expected, "message_count": len(records)})
    (ROOT / "annotation.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
