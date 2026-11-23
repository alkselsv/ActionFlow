"""Poll Telegram getUpdates and commit events before advancing the cursor."""

import argparse
import json
import sys
import time

from sqlalchemy.exc import OperationalError

from actionflow.config import Settings
from actionflow.persistence.database import database, utc_now
from actionflow.persistence.telegram import TelegramBotState
from actionflow.telegram_client import TelegramApiError, TelegramClient
from actionflow.telegram_ingest import ingest_updates


def poll_once(
    sessions,
    client: TelegramClient,
    *,
    bot_id: str,
    username: str | None = None,
    timeout: float = 30,
):
    with sessions() as db:
        state = db.get(TelegramBotState, bot_id)
        offset = (
            state.last_update_id + 1
            if state is not None and state.last_update_id is not None
            else None
        )
    updates = client.get_updates(offset=offset, timeout=timeout)
    with sessions.begin() as db:
        report = ingest_updates(
            db,
            updates,
            bot_id=bot_id,
            username=username,
        )
        state = db.get(TelegramBotState, bot_id)
        state.last_polled_at = utc_now()
        state.last_error = None
    return report


def _record_api_error(sessions, bot_id: str, error: str):
    with sessions.begin() as db:
        state = db.get(TelegramBotState, bot_id)
        if state is None:
            state = TelegramBotState(bot_id=bot_id, updated_at=utc_now())
            db.add(state)
        state.last_error = error[:100]
        state.updated_at = utc_now()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Receive new Telegram group messages")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit instead of running continuously",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Long-poll timeout in seconds",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.telegram_bot_token:
        print(
            "TELEGRAM_BOT_TOKEN is not configured. Set it in the local environment.",
            file=sys.stderr,
        )
        return 2
    timeout = args.timeout or settings.telegram_poll_timeout_seconds
    client = TelegramClient(settings.telegram_bot_token)
    try:
        bot = client.get_me()
    except TelegramApiError as error:
        print(f"Telegram bot check failed: {error}", file=sys.stderr)
        return 1

    engine, sessions = database(settings.database_url)
    try:
        while True:
            try:
                report = poll_once(
                    sessions,
                    client,
                    bot_id=str(bot["id"]),
                    username=bot.get("username"),
                    timeout=timeout,
                )
                if report.accepted or report.rejected:
                    print(
                        json.dumps(report.as_dict(), ensure_ascii=False),
                        flush=True,
                    )
            except TelegramApiError as error:
                try:
                    _record_api_error(sessions, str(bot["id"]), type(error).__name__)
                except OperationalError:
                    pass
                print(f"Telegram polling failed: {error}", file=sys.stderr)
                if args.once:
                    return 1
                time.sleep(5)
            except OperationalError:
                if args.once:
                    return 1
                time.sleep(2)
            if args.once:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
