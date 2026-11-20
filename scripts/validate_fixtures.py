#!/usr/bin/env python3
"""Validate the synthetic Telegram-style fixtures (AF-001).

The checks cover the guarantees that later import work and evidence
preservation rely on: strict UTF-8 without BOM, resolvable reply links,
stable ordering, consistent timestamps, supported message shapes and the
absence of real contact data.

Run from the repository root:

    python scripts/validate_fixtures.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "telegram"
VALID_ROLES = {"customer", "employee", "system", "unknown"}
VALID_MESSAGE_TYPES = {"message", "service"}
MEDIA_FIELDS = ("photo", "file", "voice_message", "video_message", "sticker")
VALID_MEDIA_TYPES = {
    "photo",
    "document",
    "file",
    "voice_message",
    "video_message",
    "audio",
    "video",
    "sticker",
}
PII_PATTERNS = {
    "url": re.compile(r"https?://", re.IGNORECASE),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "handle": re.compile(r"(?<![\w])@[A-Za-z0-9_]{3,}"),
    "phone": re.compile(r"\+?\d[\d\s()\-]{8,}\d"),
    "company": re.compile(r"\b(?:ООО|ОАО|ЗАО|ИП|ИНН|ОГРН|КПП)\b"),
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def message_text(message: dict, path: Path, index: int, errors: list[str]) -> str:
    text = message.get("text")
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for part in text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                fail(errors, f"{path.name}: message {index} has an unsupported text part")
        return "".join(parts)
    fail(errors, f"{path.name}: message {index} has an unsupported text value")
    return ""


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_file(path: Path, errors: list[str]) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail(errors, f"{path.name}: UTF-8 BOM is not allowed")
        raw = raw[3:]

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(errors, f"{path.name}: cannot parse JSON: {exc}")
        return {}

    fixture = payload.get("_actionflow_fixture") or {}
    if fixture.get("synthetic") is not True:
        fail(errors, f"{path.name}: fixture is not marked synthetic")
    if fixture.get("not_a_real_telegram_export") is not True:
        fail(errors, f"{path.name}: fixture does not declare that it is not a real export")

    workspace = fixture.get("workspace_id")
    conversation = fixture.get("conversation_id")
    if not workspace or not conversation:
        fail(errors, f"{path.name}: workspace_id and conversation_id are required")

    sequence = fixture.get("import_sequence")
    if sequence is not None and (not isinstance(sequence, int) or sequence < 1):
        fail(errors, f"{path.name}: import_sequence must be a positive integer")

    chat_id = payload.get("id")
    if not isinstance(chat_id, int):
        fail(errors, f"{path.name}: chat id must be an integer")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        fail(errors, f"{path.name}: messages must be a non-empty list")
        return {}

    ids: list[int] = []
    by_id: dict[int, dict] = {}
    external_ids: set[str] = set()
    externals: dict[str, dict] = {}

    for index, message in enumerate(messages):
        local_id = message.get("id")
        if not isinstance(local_id, int):
            fail(errors, f"{path.name}: message {index} has a non-integer id")
            continue
        if local_id in by_id:
            fail(errors, f"{path.name}: duplicate message id {local_id}")
        ids.append(local_id)
        by_id[local_id] = message

        for field in ("date", "date_unixtime", "type"):
            if field not in message:
                fail(errors, f"{path.name}: message {local_id} is missing {field}")

        message_type = message.get("type")
        if message_type not in VALID_MESSAGE_TYPES:
            fail(errors, f"{path.name}: message {local_id} has an unsupported type {message_type!r}")

        source = message.get("_actionflow_synthetic_source") or {}
        for field in ("external_id", "revision", "sender_role", "sent_at", "observed_at", "source"):
            if field not in source:
                fail(errors, f"{path.name}: message {local_id} is missing source.{field}")

        external_id = source.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            fail(errors, f"{path.name}: message {local_id} has no external_id")
        elif external_id in external_ids:
            fail(errors, f"{path.name}: duplicate external_id {external_id}")
        else:
            external_ids.add(external_id)

        role = source.get("sender_role")
        if role not in VALID_ROLES:
            fail(errors, f"{path.name}: message {local_id} has an invalid sender_role")
        if message_type == "service" and role != "system":
            fail(errors, f"{path.name}: service message {local_id} must use the system role")
        if source.get("source") != "import":
            fail(errors, f"{path.name}: message {local_id} must have source=import")
        if not isinstance(source.get("revision"), int) or source["revision"] < 1:
            fail(errors, f"{path.name}: message {local_id} has an invalid revision")

        from_id = message.get("from_id")
        if message_type == "message":
            if not isinstance(message.get("from"), str) or not message.get("from"):
                fail(errors, f"{path.name}: message {local_id} has no author")
            if not isinstance(from_id, str) or not from_id.startswith("user"):
                fail(errors, f"{path.name}: message {local_id} has a non-synthetic from_id")
        else:
            if not isinstance(message.get("actor"), str) or not message.get("actor"):
                fail(errors, f"{path.name}: service message {local_id} has no actor")
            if not isinstance(message.get("action"), str) or not message.get("action"):
                fail(errors, f"{path.name}: service message {local_id} has no action")

        media_type = message.get("media_type")
        if media_type is not None and media_type not in VALID_MEDIA_TYPES:
            fail(errors, f"{path.name}: message {local_id} has an unsupported media_type {media_type!r}")
        has_media = bool(media_type or any(field in message for field in MEDIA_FIELDS))
        if message_type == "message" and message.get("text") is None and not has_media:
            fail(errors, f"{path.name}: message {local_id} has neither text nor media")

        try:
            message_date = parse_utc(message["date"])
            expected_epoch = int(message["date_unixtime"])
        except (TypeError, ValueError) as exc:
            fail(errors, f"{path.name}: message {local_id} has an invalid date: {exc}")
            message_date = None
        else:
            if int(message_date.timestamp()) != expected_epoch:
                fail(errors, f"{path.name}: message {local_id} date/date_unixtime mismatch")

        try:
            sent_at = datetime.fromisoformat(source["sent_at"])
            observed_at = datetime.fromisoformat(source["observed_at"])
        except (TypeError, ValueError) as exc:
            fail(errors, f"{path.name}: message {local_id} has an invalid source timestamp: {exc}")
        else:
            if sent_at.tzinfo is None or observed_at.tzinfo is None:
                fail(errors, f"{path.name}: message {local_id} source timestamps need an offset")
            elif message_date is not None and sent_at.astimezone(timezone.utc) != message_date:
                fail(errors, f"{path.name}: message {local_id} sent_at does not match date")
            elif observed_at < sent_at:
                fail(errors, f"{path.name}: message {local_id} observed before sent")

        if "edited" in message or "edited_unixtime" in message:
            if "edited" not in message or "edited_unixtime" not in message:
                fail(errors, f"{path.name}: message {local_id} needs both edited and edited_unixtime")
            else:
                try:
                    edited_at = parse_utc(message["edited"])
                    expected_edited = int(message["edited_unixtime"])
                except (TypeError, ValueError) as exc:
                    fail(errors, f"{path.name}: message {local_id} has an invalid edited date: {exc}")
                else:
                    if int(edited_at.timestamp()) != expected_edited:
                        fail(errors, f"{path.name}: message {local_id} edited/edited_unixtime mismatch")
                    if message_date is not None and edited_at < message_date:
                        fail(errors, f"{path.name}: message {local_id} edited before sent")
                    if source.get("revision", 1) < 2:
                        fail(errors, f"{path.name}: message {local_id} is edited but has revision < 2")

        if "forwarded_from" in message and not isinstance(message["forwarded_from"], str):
            fail(errors, f"{path.name}: message {local_id} has an invalid forwarded_from")

        text = message_text(message, path, local_id, errors)
        for name, pattern in PII_PATTERNS.items():
            if pattern.search(text):
                fail(errors, f"{path.name}: message {local_id} contains possible {name} data")

        if external_id:
            externals[external_id] = {
                "revision": source.get("revision", 0),
                "observed": observed_at if "observed_at" in source else None,
            }

    if ids != sorted(ids):
        fail(errors, f"{path.name}: messages are not ordered by id")

    for message in messages:
        local_id = message.get("id")
        reply_id = message.get("reply_to_message_id")
        source = message.get("_actionflow_synthetic_source") or {}
        external_reply = source.get("reply_to_external_id")
        if reply_id is None:
            if external_reply is not None:
                fail(errors, f"{path.name}: message {local_id} has external reply without a local target")
            continue
        target = by_id.get(reply_id)
        if target is None:
            fail(errors, f"{path.name}: message {local_id} replies to missing message {reply_id}")
            continue
        target_external = (target.get("_actionflow_synthetic_source") or {}).get("external_id")
        if external_reply != target_external:
            fail(errors, f"{path.name}: message {local_id} external reply does not match target")

    return {
        "path": path,
        "workspace": workspace,
        "conversation": conversation,
        "chat_id": chat_id,
        "sequence": sequence,
        "reimport_of": fixture.get("reimport_of"),
        "externals": externals,
        "count": len(messages),
    }


def validate_reimport(base: dict, current: dict, errors: list[str]) -> None:
    missing = set(base["externals"]) - set(current["externals"])
    if missing:
        fail(
            errors,
            f"{current['path'].name}: reimport is missing {len(missing)} message(s) from {base['path'].name}",
        )
    for external_id, base_info in base["externals"].items():
        current_info = current["externals"].get(external_id)
        if not current_info:
            continue
        if current_info["revision"] < base_info["revision"]:
            fail(errors, f"{current['path'].name}: revision of {external_id} went backwards")
        if (
            base_info["observed"]
            and current_info["observed"]
            and current_info["observed"] < base_info["observed"]
        ):
            fail(errors, f"{current['path'].name}: observed_at of {external_id} went backwards")
    if base["sequence"] is not None and current["sequence"] is not None:
        if current["sequence"] <= base["sequence"]:
            fail(errors, f"{current['path'].name}: import_sequence must increase")


def main() -> int:
    if not FIXTURES_DIR.is_dir():
        print(f"Fixtures directory not found: {FIXTURES_DIR}", file=sys.stderr)
        return 2

    errors: list[str] = []
    chats: dict[tuple[str, str], dict] = {}
    reimports: list[tuple[dict, dict]] = []
    total_messages = 0
    files = sorted(FIXTURES_DIR.glob("*.json"))

    for path in files:
        info = validate_file(path, errors)
        if not info:
            continue
        total_messages += info["count"]
        key = (info["workspace"], info["conversation"])
        if key in chats:
            base = chats[key]
            if info["reimport_of"] == base["path"].name and info["chat_id"] == base["chat_id"]:
                reimports.append((base, info))
            else:
                fail(errors, f"{path.name}: duplicates conversation {key} from {base['path'].name}")
        else:
            chats[key] = info

    for base, current in reimports:
        validate_reimport(base, current, errors)
    for info in chats.values():
        if info["reimport_of"] and info["reimport_of"] not in {b["path"].name for b, _ in reimports}:
            fail(errors, f"{info['path'].name}: reimport_of does not reference an imported file")

    if errors:
        print(f"Fixture validation failed ({len(errors)} issue(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(files)} files, {len(chats)} conversations, "
        f"{total_messages} message records, {len(reimports)} re-import pair(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
