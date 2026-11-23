"""One-shot, redacted Telegram Bot API probe for AF-026 (#28).

Reads TELEGRAM_BOT_TOKEN from the process environment. It never sends a chat
message and does not print or persist the token, usernames, or message text.
Updates are never acknowledged by this probe, so a later run can receive them
again. The report is written outside the repository.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
REDACTION_KEY = os.urandom(32)


def key(value: Any) -> str | None:
    if value is None:
        return None
    return hmac.new(REDACTION_KEY, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def valid_bot_token(token: str) -> bool:
    return re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) is not None


def safe_network_error(error: BaseException) -> str:
    """Classify connection failures without exposing URLs or proxy details."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return "DNS lookup for Telegram failed."
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "Connection to Telegram timed out."
    if isinstance(reason, ssl.SSLError):
        return "TLS connection to Telegram failed."
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "Telegram endpoint returned an invalid response."
    return "Could not connect to the Telegram API."


def text_digest(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in value
        )
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def content_type(message: dict[str, Any]) -> str:
    if "text" in message:
        return "text"
    return next(
        (name for name in ("photo", "document", "audio", "voice", "video", "sticker", "location") if name in message),
        "other",
    )


def unix_seconds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None


def numeric_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def normalize_export_chat_id(chat_id: Any, chat_type: Any) -> int | None:
    """Convert a positive Telegram Desktop peer ID to Bot API dialog-ID form."""
    peer_id = numeric_id(chat_id)
    if peer_id is None:
        return None
    kind = str(chat_type or "").lower()
    if peer_id < 0:
        if "supergroup" in kind or "channel" in kind:
            if -1_997_852_516_352 <= peer_id <= -1_000_000_000_001:
                return peer_id
            return None
        if "group" in kind and -999_999_999_999 <= peer_id <= -1:
            return peer_id
        return None
    if peer_id == 0:
        return None
    if peer_id > 997_852_516_352 and "supergroup" in str(chat_type or "").lower():
        return None
    if peer_id > 999_999_999_999 and "group" in str(chat_type or "").lower():
        return None
    if "supergroup" in kind or "channel" in kind:
        if not 1 <= peer_id <= 997_852_516_352:
            return None
        return -(1_000_000_000_000 + peer_id)
    if "group" in kind:
        if not 1 <= peer_id <= 999_999_999_999:
            return None
        return -peer_id
    if kind in {"personal_chat", "private_chat", "user"}:
        return peer_id
    return None


def normalize_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for update in updates:
        event_type = next(
            (field for field in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member", "chat_member") if field in update),
            "other",
        )
        item = update.get(event_type) or {}
        chat = item.get("chat") or {}
        actor = item.get("from") or {}
        old_member = item.get("old_chat_member") or {}
        new_member = item.get("new_chat_member") or {}
        is_message = event_type in {"message", "edited_message", "channel_post", "edited_channel_post"}
        rows.append({
            "update_id": update.get("update_id"),
            "event_type": event_type,
            "chat_key": key(chat.get("id")),
            "chat_id_value": numeric_id(chat.get("id")),
            "chat_type": chat.get("type"),
            "message_id": item.get("message_id") if is_message else None,
            "reply_to_message_id": (item.get("reply_to_message") or {}).get("message_id") if is_message else None,
            "actor_key": key(actor.get("id")),
            "date": item.get("date"),
            "edit_date": item.get("edit_date"),
            "content_type": content_type(item) if is_message else None,
            "text_sha256": text_digest(item.get("text", item.get("caption"))) if is_message else None,
            "old_status": old_member.get("status") if event_type in {"my_chat_member", "chat_member"} else None,
            "new_status": new_member.get("status") if event_type in {"my_chat_member", "chat_member"} else None,
        })
    return rows


def normalize_export(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_chat_id = numeric_id(data.get("id"))
    chat_id = normalize_export_chat_id(raw_chat_id, data.get("type"))
    rows = []
    for item in data.get("messages", []):
        if item.get("type") != "message":
            continue
        reply_id = numeric_id(item.get("reply_to_message_id"))
        if reply_id is None:
            reply_id = numeric_id((item.get("reply_to") or {}).get("message_id"))
        rows.append({
            "chat_key": key(chat_id),
            "chat_id_value": chat_id,
            "raw_chat_id_value": raw_chat_id,
            "message_id": numeric_id(item.get("id")),
            "reply_to_message_id": reply_id,
            "actor_key": key((re.search(r"(\d+)$", str(item.get("from_id", ""))) or [None, None])[1]),
            "date": unix_seconds(item.get("date_unixtime") or item.get("date")),
            "edit_date": unix_seconds(item.get("edit_date") or item.get("edited")),
            "content_type": "text" if "text" in item else next((x for x in ("photo", "file", "audio", "voice_message", "video_file", "sticker_emoji") if x in item), "other"),
            "text_sha256": text_digest(item.get("text", item.get("caption"))),
        })
    return rows


def compare_export(export_rows: list[dict[str, Any]], update_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare likely matching messages without storing content or its digest."""
    comparisons = []
    message_updates = [row for row in update_rows if row.get("message_id") is not None]
    for item in export_rows:
        same_message_id = [row for row in message_updates if row["message_id"] == item["message_id"]]
        candidates = [row for row in same_message_id if row.get("chat_key") == item.get("chat_key")]
        exact = [row for row in candidates if
                 row.get("text_sha256") == item.get("text_sha256") and
                 row.get("reply_to_message_id") == item.get("reply_to_message_id") and
                 row.get("date") == item.get("date")]
        match = exact[0] if len(exact) == 1 else None
        comparisons.append({
            "message_key": key(f'{item.get("chat_key")}:{item["message_id"]}'),
            "reply_message_key": key(f'{item.get("chat_key")}:{item["reply_to_message_id"]}') if item.get("reply_to_message_id") is not None else None,
            "chat_id_exact_match": any(item.get("raw_chat_id_value") == row.get("chat_id_value") for row in candidates),
            "chat_id_converted_match": bool(candidates),
            "same_message_id_count": len(same_message_id),
            "same_chat_message_id_count": len(candidates),
            "match_status": "matched" if match else "ambiguous" if candidates else "chat_id_mismatch" if same_message_id else "not_observed",
            "message_id_equal": bool(match) and item.get("message_id") == match.get("message_id"),
            "reply_key_equal": bool(match) and item.get("reply_to_message_id") == match.get("reply_to_message_id"),
            "author_key_equal": bool(match) and item.get("actor_key") == match.get("actor_key"),
            "timestamp_equal": bool(match) and item.get("date") == match.get("date"),
            "edit_timestamp_equal": bool(match) and item.get("edit_date") == match.get("edit_date"),
            "text_equal": bool(match),
        })
    return comparisons


def report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pseudonymize IDs and drop message text digests before serialization."""
    reports = []
    for row in rows:
        redacted = {name: value for name, value in row.items()
                    if name not in {"text_sha256", "update_id", "message_id", "reply_to_message_id", "chat_id_value", "raw_chat_id_value"}}
        if row.get("update_id") is not None:
            redacted["update_key"] = key(row["update_id"])
        if row.get("message_id") is not None:
            redacted["message_key"] = key(f'{row.get("chat_key")}:{row["message_id"]}')
        if row.get("reply_to_message_id") is not None:
            redacted["reply_message_key"] = key(f'{row.get("chat_key")}:{row["reply_to_message_id"]}')
        reports.append(redacted)
    return reports


def api(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API HTTP {exc.code}; response omitted to protect credentials") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException) as exc:
        raise RuntimeError(safe_network_error(exc)) from None
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected {method}; error details suppressed")
    return payload["result"]


def default_output() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ActionFlow" / "telegram-access"
    return base / f"af026-probe-{int(time.time())}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output())
    parser.add_argument("--timeout", type=int, default=5, choices=range(0, 31))
    parser.add_argument("--export", type=Path, help="optional Telegram Desktop JSON export for this test group")
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and sys.stdin.isatty():
        token = getpass.getpass("Type Telegram bot token (hidden): ").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN or run in an interactive terminal to enter it securely.", file=sys.stderr)
        return 2
    if not valid_bot_token(token):
        print("Bot token format is invalid; no Telegram request was sent. Re-enter it without spaces or control characters.", file=sys.stderr)
        return 2
    if REPO in args.output.resolve().parents or args.output.resolve() == REPO:
        print("Output must be outside the Git repository.", file=sys.stderr)
        return 2
    if args.export and (REPO in args.export.resolve().parents or args.export.resolve() == REPO):
        print("Telegram export must be outside the Git repository.", file=sys.stderr)
        return 2
    try:
        webhook = api(token, "getWebhookInfo")
        if webhook.get("url"):
            print("A webhook is configured; getUpdates cannot be used until it is removed.", file=sys.stderr)
            return 2
        me = api(token, "getMe")
        updates = api(token, "getUpdates", {
            "timeout": args.timeout,
            "allowed_updates": json.dumps(["message", "edited_message", "my_chat_member"]),
        })
        rows = normalize_updates(updates)
        report: dict[str, Any] = {
            "bot": {"id_key": key(me.get("id")), "is_bot": me.get("is_bot")},
            "transport": "getUpdates long polling",
            "webhook_configured": False,
            "updates": report_rows(rows),
        }
        if args.export:
            export_rows = normalize_export(args.export)
            report["export_comparison"] = compare_export(export_rows, rows)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Could not read the export or write the report; check the paths.", file=sys.stderr)
        return 2
    print(f"Saved redacted probe report: {args.output}")
    print(f"Captured {len(rows)} update(s); message text and usernames were not written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
