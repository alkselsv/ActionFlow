"""Poll Telegram getUpdates and commit events before advancing the cursor."""

import argparse
import json
import sys
import time
from datetime import timedelta

from sqlalchemy.exc import OperationalError

from actionflow.config import Settings
from actionflow.persistence.database import as_utc, database, utc_now
from actionflow.persistence.telegram import TelegramBotState
from actionflow.telegram_client import (
    UPDATE_LIMIT,
    TelegramApiError,
    TelegramClient,
    configured_bot_id,
)
from actionflow.telegram_ingest import ingest_updates
from actionflow.telegram_recovery import RETENTION, record_retention_gap

MAX_ATTEMPTS = 5


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
        last_update = as_utc(state.last_update_at) if state else None
        offset = (
            state.last_update_id + 1
            if state is not None
            and state.last_update_id is not None
            and last_update is not None
            and utc_now() - last_update < RETENTION
            else None
        )
    updates = client.get_updates(offset=offset, timeout=timeout)
    now = utc_now()
    with sessions.begin() as db:
        previous = db.get(TelegramBotState, bot_id)
        if len(updates) < UPDATE_LIMIT:
            record_retention_gap(
                db, as_utc(previous.last_caught_up_at) if previous else None, now
            )
        report = ingest_updates(
            db,
            updates,
            bot_id=bot_id,
            username=username,
            now=now,
        )
        state = db.get(TelegramBotState, bot_id)
        state.last_polled_at = now
        if len(updates) < UPDATE_LIMIT:
            state.last_caught_up_at = now
        state.last_error = None
        state.failures = 0
        state.retry_at = None
    return report


def _record_api_error(
    sessions, bot_id: str, code: str, attempt: int, delay: int | None
):
    with sessions.begin() as db:
        state = db.get(TelegramBotState, bot_id)
        if state is None:
            state = TelegramBotState(bot_id=bot_id, updated_at=utc_now())
            db.add(state)
        now = utc_now()
        state.last_error = code
        state.failures = attempt
        state.retry_at = now + timedelta(seconds=delay) if delay is not None else None
        state.updated_at = now


def run_polling(
    sessions,
    client,
    *,
    bot_id,
    username=None,
    timeout=30,
    once=False,
    sleep=time.sleep,
    verify_identity=False,
):
    check_cooldown = True
    attempt = 0
    while True:
        try:
            if check_cooldown:
                with sessions() as db:
                    state = db.get(TelegramBotState, bot_id)
                    retry_at = as_utc(state.retry_at) if state else None
                if retry_at is not None and retry_at > utc_now():
                    if once:
                        return 1
                    sleep(max(0, (retry_at - utc_now()).total_seconds()))
                check_cooldown = False
            if verify_identity:
                bot = client.get_me()
                if str(bot["id"]) != bot_id:
                    raise TelegramApiError("Wrong bot identity.", code="configuration")
                username = bot.get("username")
                verify_identity = False
            report = poll_once(
                sessions, client, bot_id=bot_id, username=username, timeout=timeout
            )
        except (TelegramApiError, OperationalError) as error:
            attempt += 1
            code = (
                error.code
                if isinstance(error, TelegramApiError)
                else "database_unavailable"
            )
            retryable = code in {"temporary", "rate_limited", "database_unavailable"}
            minimum = (
                (error.retry_after or 0) if isinstance(error, TelegramApiError) else 0
            )
            delay = (
                max(minimum, 2**attempt)
                if retryable and attempt < MAX_ATTEMPTS
                else None
            )
            try:
                _record_api_error(
                    sessions,
                    bot_id,
                    code,
                    attempt,
                    delay if delay is not None else minimum or None,
                )
            except OperationalError:
                pass
            print(
                f"Telegram polling: {code}; attempt {attempt}/{MAX_ATTEMPTS}",
                file=sys.stderr,
                flush=True,
            )
            if once or delay is None:
                return 1
            sleep(delay)
        else:
            attempt = 0
            if report.accepted or report.rejected:
                print(json.dumps(report.as_dict(), ensure_ascii=False), flush=True)
            if once:
                return 0


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
    bot_id = configured_bot_id(settings.telegram_bot_token)
    if bot_id is None:
        print(
            "Set a valid TELEGRAM_BOT_TOKEN in the local environment.",
            file=sys.stderr,
        )
        return 2
    timeout = args.timeout or settings.telegram_poll_timeout_seconds
    client = TelegramClient(settings.telegram_bot_token)
    engine, sessions = database(settings.database_url)
    try:
        return run_polling(
            sessions,
            client,
            bot_id=bot_id,
            verify_identity=True,
            timeout=timeout,
            once=args.once,
        )
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
