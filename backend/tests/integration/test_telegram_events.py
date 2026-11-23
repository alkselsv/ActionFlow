import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

import actionflow.telegram_ingest as ingest_module
from actionflow.api.app import create_app
from actionflow.application.analysis_input import prepare_input
from actionflow.config import Settings
from actionflow.dispatcher import dispatch_once
from actionflow.persistence.analysis import (
    AnalysisInputMessage,
    AnalysisRun,
    OutboxEvent,
)
from actionflow.persistence.database import as_utc, database
from actionflow.persistence.project_settings import Chat, Message, MessageRevision
from actionflow.persistence.telegram import (
    TelegramBotState,
    TelegramConnection,
)
from actionflow.setup_manager import create_manager
from actionflow.telegram_client import TelegramApiError
from actionflow.telegram_connector import poll_once, run_polling
from actionflow.telegram_ingest import ingest_updates
from actionflow.telegram_recovery import open_gap

ORIGIN = {"Origin": "http://127.0.0.1:5173"}
PASSWORD = "synthetic-telegram-password"
CHAT_A = "-1001111111111"
CHAT_B = "-1002222222222"


def make_app(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'telegram.db'}")
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    migration = Config(__file__.rsplit("backend", 1)[0] + "backend/alembic.ini")
    command.upgrade(migration, "head")
    _, sessions = database(settings.database_url)
    create_manager(sessions, "manager", PASSWORD)
    return settings, sessions


def login(browser):
    response = browser.post(
        "/api/v1/auth/login",
        json={"username": "manager", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 200


def create_project(browser, name):
    response = browser.post(
        "/api/v1/projects",
        json={
            "client_name": "Synthetic Telegram client",
            "project_name": name,
            "manager_name": "Manager",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201
    return response.json()["id"]


def connect(browser, project_id, chat_id=CHAT_A):
    response = browser.post(
        f"/api/v1/projects/{project_id}/telegram/connect",
        headers=ORIGIN,
        json={"chat_id": chat_id, "name": "Synthetic test group"},
    )
    assert response.status_code == 200
    return response.json()


def message(chat_id, message_id, text, *, author=101, date=1_800_000_000, reply=None):
    value = {
        "message_id": message_id,
        "date": date,
        "chat": {"id": int(chat_id), "type": "supergroup", "title": "Synthetic"},
        "from": {"id": author, "first_name": "Synthetic", "last_name": "Author"},
        "text": text,
    }
    if reply is not None:
        value["reply_to_message"] = {"message_id": reply}
    return value


def update(update_id, payload, kind="message"):
    return {"update_id": update_id, kind: payload}


@pytest.mark.parametrize("respond_at_deadline", [False, True])
def test_timer_applies_current_state_once_after_worker_restart(
    tmp_path, monkeypatch, respond_at_deadline
):
    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from actionflow.persistence.analysis import AnalysisRun
    from actionflow.persistence.database import as_utc
    from actionflow.persistence.findings import Commitment, DeadlineObservation, Finding
    from actionflow.persistence.project_settings import (
        MessageRevision,
        ParticipantSide,
        SettingsVersion,
    )
    from actionflow.temporal.deadlines import TelegramDeadlineWindow
    from actionflow.worker import analysis_worker

    if os.environ.get("ACTIONFLOW_TEST_TEMPORAL") != "1":
        pytest.skip("Set ACTIONFLOW_TEST_TEMPORAL=1 to start the Temporal test server")
    monkeypatch.setattr(
        "actionflow.worker.Worker",
        lambda *args, **kwargs: Worker(
            *args,
            **kwargs,
            max_cached_workflows=0,
        ),
    )
    settings, sessions = make_app(tmp_path, monkeypatch)

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as temporal:
            at = await temporal.get_current_time()
            due_at = at + timedelta(seconds=12)
            with TestClient(create_app(settings)) as browser:
                login(browser)
                project = create_project(browser, "Deadline timer")
                connect(browser, project, CHAT_A)
                with sessions.begin() as db:
                    ingest_updates(
                        db,
                        [
                            update(
                                40,
                                message(
                                    CHAT_A,
                                    1,
                                    "Когда будет готово?",
                                    date=int(at.timestamp()) - 60,
                                ),
                            )
                        ],
                        bot_id="deadline-bot",
                        now=at,
                    )
                    anchor = db.scalar(select(Message))
                    revision = db.scalar(select(MessageRevision))
                    version = SettingsVersion(
                        project_id=project,
                        version_no=1,
                        context_text="",
                        agreements="",
                        manager_name="Manager",
                        client_type="north",
                        work_hours={
                            "weekdays": [1, 2, 3, 4, 5],
                            "start": "09:00",
                            "end": "18:00",
                        },
                        calendar={"closed_dates": []},
                        timezone="Europe/Moscow",
                        first_response_minutes=60,
                        created_at=at - timedelta(days=1),
                        sides=[ParticipantSide(source_author_id="102", side="vedita")],
                    )
                    db.add(version)
                    db.flush()
                    run = AnalysisRun(
                        project_id=project,
                        chat_id=anchor.chat_id,
                        period_start=at - timedelta(minutes=2),
                        period_end=at,
                        as_of=at,
                        apply_to_queue=True,
                        status="succeeded",
                        request_hash="test",
                        workflow_id="deadline-history",
                        settings_version_id=version.id,
                    )
                    db.add(run)
                    db.flush()
                    finding = Finding(
                        project_id=project,
                        finding_type="unanswered",
                        subject_kind="message",
                        subject_id=anchor.id,
                        anchor_message_id=anchor.id,
                        anchor_revision_id=revision.id,
                        description="Open question",
                        status="confirmed",
                        confidence=0.8,
                        priority="medium",
                        sla_due_at=due_at,
                        sla_state="pending",
                        current_run_id=run.id,
                    )
                    db.add(finding)
                    db.flush()
                    finding_id = finding.id
                    promise = Commitment(
                        project_id=project,
                        anchor_message_id=anchor.id,
                        anchor_revision_id=revision.id,
                        subject_text="Promise",
                        status="open",
                        due_at=due_at,
                    )
                    db.add(promise)
                    db.flush()
                    commitment_card = Finding(
                        project_id=project,
                        finding_type="commitment",
                        subject_kind="commitment",
                        subject_id=promise.id,
                        anchor_message_id=anchor.id,
                        anchor_revision_id=revision.id,
                        description="Promise",
                        priority="low",
                        confidence=0.8,
                        commitment_due_at=due_at,
                        commitment_status="open",
                        current_run_id=run.id,
                    )
                    db.add(commitment_card)
                    db.flush()
                    commitment_id = commitment_card.id
                    version_id = version.id
                queue = "deadline-test"
                with ThreadPoolExecutor(4) as executor:
                    async with analysis_worker(
                        temporal.client, sessions, queue, executor
                    ):
                        handle = await temporal.client.start_workflow(
                            TelegramDeadlineWindow.run,
                            30,
                            id="deadline-one",
                            task_queue=queue,
                        )
                        for _ in range(100):
                            history = await handle.fetch_history()
                            if any(
                                event.HasField("timer_started_event_attributes")
                                for event in history.events
                            ):
                                break
                            await asyncio.sleep(0.05)
                        else:
                            pytest.fail("A durable Temporal timer was not scheduled")
                    if respond_at_deadline:
                        with sessions.begin() as db:
                            ingest_updates(
                                db,
                                [
                                    update(
                                        41,
                                        message(
                                            CHAT_A,
                                            2,
                                            "Принял, вернусь с ответом.",
                                            author=102,
                                            reply=1,
                                            date=int(due_at.timestamp()),
                                        ),
                                    )
                                ],
                                bot_id="deadline-bot",
                                now=due_at,
                            )
                    async with analysis_worker(
                        temporal.client, sessions, queue, executor
                    ):
                        await asyncio.wait_for(handle.result(), 25)
                        await asyncio.wait_for(
                            temporal.client.execute_workflow(
                                TelegramDeadlineWindow.run,
                                15,
                                id="deadline-repeat",
                                task_queue=queue,
                            ),
                            25,
                        )
                card = browser.get(f"/api/v1/findings/{finding_id}").json()
                assert card["status"] == "confirmed"
                assert card["sla"]["state"] == (
                    "met" if respond_at_deadline else "breached"
                )
                assert card["priority"] == ("medium" if respond_at_deadline else "high")
                assert card["sla"]["settings_version_id"] == version_id
                assert card["sla"]["reason"]["trigger"] == "deadline_timer"
                commitment = browser.get(f"/api/v1/findings/{commitment_id}").json()
                assert commitment["priority"] == "high"
                assert commitment["commitment"]["status"] == "open"
                assert commitment["deadline_checks"][0]["state"] == "overdue"
                with sessions() as db:
                    [check] = db.scalars(
                        select(DeadlineObservation).where(
                            DeadlineObservation.finding_id == finding_id
                        )
                    )
                    assert as_utc(check.evaluated_at) >= due_at
                    assert as_utc(check.due_at) == due_at

    asyncio.run(scenario())


def test_scheduled_reconciliation_is_idempotent_and_skips_disconnected_chat(
    tmp_path, monkeypatch
):
    from actionflow.application.deadlines import reconcile
    from actionflow.persistence.analysis import AnalysisRun
    from actionflow.persistence.telegram import TelegramConnection
    from actionflow.telegram_schedules import DEADLINE_ID, RECONCILIATION_ID, schedules

    settings, sessions = make_app(tmp_path, monkeypatch)
    at = datetime.now(UTC)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Scheduled reconciliation")
        connect(browser, project, CHAT_A)
        with sessions.begin() as db:
            ingest_updates(
                db,
                [
                    update(
                        2,
                        message(
                            CHAT_A, 1, "Known history", date=int(at.timestamp()) - 60
                        ),
                    )
                ],
                bot_id="reconcile-test",
                now=at,
            )
        assert reconcile(sessions, at, 12345) == 1
        with sessions.begin() as db:
            run = db.scalar(select(AnalysisRun))
            run.status = "succeeded"
            assert run.idempotency_key == "telegram-schedule:12345"
        assert reconcile(sessions, at, 12345) == 0
        with sessions.begin() as db:
            db.scalar(select(TelegramConnection)).status = "disconnected"
        assert reconcile(sessions, at, 12346) == 0
    configured = schedules("test-queue", 60)
    assert set(configured) == {DEADLINE_ID, RECONCILIATION_ID}
    assert configured[RECONCILIATION_ID].spec.intervals[0].every == timedelta(hours=1)
    assert configured[RECONCILIATION_ID].policy.overlap.name == "SKIP"


def test_recovery_retains_late_events_and_marks_expired_history(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    clock = datetime.now(UTC) - timedelta(days=2)
    monkeypatch.setattr("actionflow.telegram_connector.utc_now", lambda: clock)
    monkeypatch.setattr("actionflow.api.telegram.utc_now", lambda: clock)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Recovery")
        connect(browser, project, CHAT_A)

        event = update(40, message(CHAT_A, 1, "First"))
        late = update(39, message(CHAT_A, 2, "Late delivery"))
        reset = update(3, message(CHAT_A, 3, "After retention"))
        backlog = [
            reset,
            *(update(i, message(CHAT_B, i, "Other chat")) for i in range(4, 103)),
        ]
        client = FakeClient([[event], [late, event], backlog, []])
        poll_once(sessions, client, bot_id="test-bot")
        clock += timedelta(minutes=30)
        # A new polling call reads the committed cursor from the database.
        report = poll_once(sessions, client, bot_id="test-bot")
        assert report.last_update_id == 40
        assert report.accepted == 1
        assert (
            browser.get(f"/api/v1/projects/{project}/telegram").json()["connection"][
                "gaps"
            ]
            == []
        )

        clock += timedelta(hours=26)
        connection = browser.get(f"/api/v1/projects/{project}/telegram").json()[
            "connection"
        ]
        assert connection["polling_delayed"]
        assert connection["gaps"][0]["reason"] == "retention_expired"
        poll_once(sessions, client, bot_id="test-bot")
        connection = browser.get(f"/api/v1/projects/{project}/telegram").json()[
            "connection"
        ]
        assert not connection["polling_delayed"]
        assert connection["queue_caught_up_at"] != connection["last_polled_at"]
        assert connection["gaps"][0]["reason"] == "retention_expired"
        poll_once(sessions, client, bot_id="test-bot")
        assert client.offsets == [None, 41, None, 103]
        connection = browser.get(f"/api/v1/projects/{project}/telegram").json()[
            "connection"
        ]
        assert not connection["polling_delayed"]
        assert connection["completeness"] == "unknown"
        assert len(connection["gaps"]) == 1
        assert connection["gaps"][0]["reason"] == "retention_expired"
        messages = browser.get(f"/api/v1/projects/{project}/messages").json()["items"]
        assert {item["text"] for item in messages} == {
            "First",
            "Late delivery",
            "After retention",
        }


def test_retry_is_bounded_and_respects_telegram_rate_limit(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    delays = []

    class FailingClient:
        def __init__(self, failures):
            self.failures = iter(failures)

        def get_updates(self, **kwargs):
            raise next(self.failures)

    temporary = TelegramApiError("Temporary")
    limited = TelegramApiError("Limited", code="rate_limited", retry_after=25)
    client = FailingClient([temporary, limited, temporary, temporary, temporary])
    assert run_polling(sessions, client, bot_id="test-bot", sleep=delays.append) == 1
    assert delays == [2, 25, 8, 16]
    with sessions() as db:
        state = db.get(TelegramBotState, "test-bot")
        assert state.failures == 5
        assert state.last_error == "temporary"
        assert state.retry_at is None

    permanent = TelegramApiError("Denied", code="access_denied")
    assert (
        run_polling(
            sessions, FailingClient([permanent]), bot_id="test-bot", sleep=delays.append
        )
        == 1
    )
    with sessions() as db:
        state = db.get(TelegramBotState, "test-bot")
        assert state.failures == 1
        assert state.last_error == "access_denied"
    assert delays == [2, 25, 8, 16]
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Polling health")
        status = connect(browser, project, CHAT_A)["connection"]
        assert status["polling_error"] == "access_denied"
        assert status["polling_failures"] == 1


def test_startup_respects_persisted_retry_before_contacting_telegram(
    tmp_path, monkeypatch
):
    _, sessions = make_app(tmp_path, monkeypatch)
    with sessions.begin() as db:
        db.add(
            TelegramBotState(
                bot_id="test-bot", retry_at=datetime.now(UTC) + timedelta(minutes=2)
            )
        )
    # No Telegram methods may be called during the persisted pause.
    assert (
        run_polling(
            sessions, object(), bot_id="test-bot", once=True, verify_identity=True
        )
        == 1
    )


def test_manual_connection_and_ingestion_keep_project_boundaries(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        first = create_project(browser, "First")
        second = create_project(browser, "Second")
        assert connect(browser, first)["connection"]["chat_id"] == CHAT_A
        assert connect(browser, second, CHAT_B)["connection"]["chat_id"] == CHAT_B

        assert (
            browser.post(
                f"/api/v1/projects/{second}/telegram/connect",
                headers=ORIGIN,
                json={"chat_id": CHAT_A},
            ).status_code
            == 409
        )
        assert (
            browser.post(
                f"/api/v1/projects/{first}/telegram/connect",
                headers=ORIGIN,
                json={"chat_id": "not-an-id"},
            ).status_code
            == 422
        )
        status = browser.get(f"/api/v1/projects/{first}/telegram").json()
        assert status["connection"]["status"] == "active"
        assert status["bot_configured"] is False

        updates = [
            update(1, message(CHAT_A, 1, "Нужен счёт")),
            update(2, message(CHAT_B, 1, "Другой проект", author=201)),
            update(3, message("-1009999999999", 1, "Чужой чат", author=301)),
            update(4, message(CHAT_A, 2, "Пришлю сегодня", author=102, reply=1)),
            update(
                5,
                {
                    "message_id": 3,
                    "date": 1_800_000_300,
                    "chat": {"id": int(CHAT_A), "type": "supergroup"},
                    "from": {"id": 101},
                    "sticker": {"file_id": "synthetic-sticker"},
                },
            ),
            update(
                6,
                {
                    **message(CHAT_A, 1, "Нужен счёт до пятницы"),
                    "edit_date": 1_800_000_400,
                },
                kind="edited_message",
            ),
        ]
        with sessions.begin() as db:
            report = ingest_updates(
                db, updates, bot_id="test-bot", username="synthetic_bot"
            )
        assert report.received == 6
        assert report.accepted == 5
        assert report.rejected == 1
        assert report.rejected_reasons == {"unknown_chat": 1}
        assert report.messages_created == 4
        assert report.revisions_created == 1
        assert report.non_text == 1
        assert report.last_update_id == 6

        first_messages = browser.get(f"/api/v1/projects/{first}/messages").json()[
            "items"
        ]
        second_messages = browser.get(f"/api/v1/projects/{second}/messages").json()[
            "items"
        ]
        assert len(first_messages) == 3
        assert len(second_messages) == 1
        assert {item["external_id"] for item in first_messages} == {"1", "2", "3"}
        assert all(item["source"] == "telegram_bot" for item in first_messages)
        assert (
            next(item for item in first_messages if item["external_id"] == "2")[
                "reply_to_external_id"
            ]
            == "1"
        )
        sticker = next(item for item in first_messages if item["external_id"] == "3")
        assert sticker["attachments"]["media_type"] == "sticker"
        assert sticker["text"] == ""
        assert (
            next(item for item in first_messages if item["external_id"] == "1")["text"]
            == "Нужен счёт до пятницы"
        )

        revisions = browser.get(
            f"/api/v1/projects/{first}/messages/"
            f"{next(item for item in first_messages if item['external_id'] == '1')['id']}/revisions"
        ).json()["items"]
        assert [item["revision_no"] for item in revisions] == [1, 2]
        assert revisions[0]["text"] == "Нужен счёт"
        assert revisions[1]["text"] == "Нужен счёт до пятницы"

        participants = browser.get(f"/api/v1/projects/{first}/participants").json()[
            "items"
        ]
        assert {item["source_author_id"] for item in participants} == {"101", "102"}
        assert all(item["side"] == "unknown" for item in participants)
        saved = browser.post(
            f"/api/v1/projects/{first}/settings",
            headers=ORIGIN,
            json={
                "context_text": "",
                "agreements": "",
                "manager_name": "Manager",
                "client_type": "custom",
                "work_hours": {
                    "weekdays": [1, 2, 3, 4, 5],
                    "start": "09:00",
                    "end": "18:00",
                },
                "timezone": "Europe/Moscow",
                "first_response_minutes": 60,
                "calendar": {"closed_dates": []},
                "participant_sides": [{"source_author_id": "101", "side": "customer"}],
            },
        )
        assert saved.status_code == 201
        participants = browser.get(f"/api/v1/projects/{first}/participants").json()[
            "items"
        ]
        assert (
            next(item for item in participants if item["source_author_id"] == "101")[
                "side"
            ]
            == "customer"
        )

        with sessions.begin() as db:
            repeated = ingest_updates(
                db, updates, bot_id="test-bot", username="synthetic_bot"
            )
        assert repeated.accepted == 0
        assert repeated.rejected == 6
        assert repeated.rejected_reasons == {"duplicate_or_stale_update": 6}
        assert (
            len(browser.get(f"/api/v1/projects/{first}/messages").json()["items"]) == 3
        )

        with sessions.begin() as db:
            state = db.get(TelegramBotState, "test-bot")
            assert state.last_update_id == 6
            assert state.username == "synthetic_bot"

        assert (
            browser.post(
                f"/api/v1/projects/{first}/telegram/disconnect",
                headers=ORIGIN,
                json={},
            ).status_code
            == 200
        )
        with sessions.begin() as db:
            inactive = ingest_updates(
                db,
                [update(7, message(CHAT_A, 4, "После отключения"))],
                bot_id="test-bot",
            )
        assert inactive.rejected_reasons == {"inactive_connection": 1}
        assert (
            len(browser.get(f"/api/v1/projects/{first}/messages").json()["items"]) == 3
        )
        assert (
            browser.get(f"/api/v1/projects/{first}/telegram").json()["connection"][
                "status"
            ]
            == "disconnected"
        )


def test_invalid_events_and_membership_are_visible_and_do_not_swap_chats(
    tmp_path, monkeypatch
):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Validation")
        connect(browser, project, CHAT_A)
        bad = update(
            10,
            {
                "message_id": 1,
                "chat": {"id": int(CHAT_A), "type": "supergroup"},
                "from": {"id": 1},
                "text": "Без времени",
            },
        )
        good = update(11, message(CHAT_A, 2, "Валидное сообщение"))
        with sessions.begin() as db:
            report = ingest_updates(db, [bad, good], bot_id="test-bot")
        assert report.accepted == 1
        assert report.rejected == 1
        assert report.rejected_reasons == {"malformed_message": 1}
        status = browser.get(f"/api/v1/projects/{project}/telegram").json()
        assert status["connection"]["status"] == "needs_check"
        assert status["connection"]["last_error"] == "malformed_message"

        left = {
            "update_id": 12,
            "my_chat_member": {
                "chat": {"id": int(CHAT_A), "type": "supergroup"},
                "new_chat_member": {"status": "left"},
            },
        }
        with sessions.begin() as db:
            ingest_updates(db, [left], bot_id="test-bot")
        status = browser.get(f"/api/v1/projects/{project}/telegram").json()
        assert status["connection"]["status"] == "needs_check"
        assert status["connection"]["last_error"] == "bot_left"
        assert status["connection"]["gaps"][0]["reason"] == "access_lost"
        assert status["connection"]["gaps"][0]["end_at"] is None

        joined = {
            "update_id": 13,
            "my_chat_member": {
                "chat": {"id": int(CHAT_A), "type": "supergroup"},
                "new_chat_member": {"status": "member"},
            },
        }
        with sessions.begin() as db:
            ingest_updates(db, [joined], bot_id="test-bot")
        status = browser.get(f"/api/v1/projects/{project}/telegram").json()
        assert status["connection"]["status"] == "active"
        assert status["connection"]["last_error"] is None
        assert status["connection"]["gaps"][0]["end_at"] is not None
        assert [
            item["text"]
            for item in browser.get(f"/api/v1/projects/{project}/messages").json()[
                "items"
            ]
        ] == ["Валидное сообщение"]


def test_partial_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Partial")
        connect(browser, project)
        original = ingest_module._store_event
        calls = []

        def failing(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("simulated write failure")
            return original(*args, **kwargs)

        updates = [
            update(20, message(CHAT_A, 1, "Первое")),
            update(21, message(CHAT_A, 2, "Второе")),
        ]
        monkeypatch.setattr(ingest_module, "_store_event", failing)
        with pytest.raises(RuntimeError), sessions.begin() as db:
            ingest_updates(db, updates, bot_id="partial-bot")
        monkeypatch.setattr(ingest_module, "_store_event", original)

        with sessions() as db:
            assert db.scalar(select(Message)) is None
            assert db.get(TelegramBotState, "partial-bot") is None
        with sessions.begin() as db:
            report = ingest_updates(db, updates, bot_id="partial-bot")
        assert report.messages_created == 2
        assert report.last_update_id == 21
        assert (
            len(browser.get(f"/api/v1/projects/{project}/messages").json()["items"])
            == 2
        )


def test_import_and_bot_overlap_does_not_duplicate_or_destroy_revision(
    tmp_path, monkeypatch
):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Overlap")
        export = {
            "id": CHAT_A,
            "name": "Synthetic export group",
            "type": "supergroup",
            "messages": [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-11-23T10:00:00",
                    "date_unixtime": "1800000000",
                    "from": "Synthetic Author",
                    "from_id": "101",
                    "text": "Исходный вопрос",
                }
            ],
        }
        imported = browser.post(
            f"/api/v1/projects/{project}/messages/import",
            headers={**ORIGIN, "Content-Type": "application/json"},
            content=__import__("json").dumps(export).encode(),
        )
        assert imported.status_code == 200
        connect(browser, project, CHAT_A)
        original = update(30, message(CHAT_A, 1, "Исходный вопрос"))
        edited = update(
            31,
            {
                **message(CHAT_A, 1, "Исходный вопрос уточнён"),
                "edit_date": 1_800_000_300,
            },
            kind="edited_message",
        )
        with sessions.begin() as db:
            first = ingest_updates(db, [original], bot_id="overlap-bot")
            second = ingest_updates(db, [edited], bot_id="overlap-bot")
        assert first.messages_created == 0
        assert first.revisions_created == 0
        assert first.unchanged == 1
        assert second.revisions_created == 1
        messages = browser.get(f"/api/v1/projects/{project}/messages").json()["items"]
        assert len(messages) == 1
        assert messages[0]["text"] == "Исходный вопрос уточнён"
        revisions = browser.get(
            f"/api/v1/projects/{project}/messages/{messages[0]['id']}/revisions"
        ).json()["items"]
        assert [item["text"] for item in revisions] == [
            "Исходный вопрос",
            "Исходный вопрос уточнён",
        ]
        replay = browser.post(
            f"/api/v1/projects/{project}/messages/import",
            headers={**ORIGIN, "Content-Type": "application/json"},
            content=__import__("json").dumps(export).encode(),
        )
        assert replay.status_code == 200
        assert browser.get(f"/api/v1/projects/{project}/telegram").json()["connection"][
            "last_reconciled_at"
        ]
        assert replay.json()["imported"] == 0
        assert (
            len(browser.get(f"/api/v1/projects/{project}/messages").json()["items"])
            == 1
        )


def test_same_second_edits_remain_ordered_despite_late_delivery(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Same-second edits")
        connect(browser, project)
        newer = update(
            12,
            {**message(CHAT_A, 1, "Latest"), "edit_date": 1_800_000_300},
            "edited_message",
        )
        older = update(
            11,
            {**message(CHAT_A, 1, "Earlier"), "edit_date": 1_800_000_300},
            "edited_message",
        )
        with sessions.begin() as db:
            ingest_updates(db, [newer], bot_id="test-bot")
        with sessions.begin() as db:
            ingest_updates(db, [older], bot_id="test-bot")
        [current] = browser.get(f"/api/v1/projects/{project}/messages").json()["items"]
        assert current["text"] == "Latest"
        revisions = browser.get(
            f"/api/v1/projects/{project}/messages/{current['id']}/revisions"
        ).json()["items"]
        assert {revision["text"] for revision in revisions} == {"Earlier", "Latest"}


def test_changed_telegram_events_coalesce_into_one_pending_analysis(
    tmp_path, monkeypatch
):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Event-driven analysis")
        connect(browser, project)
        now = datetime.now(UTC)
        initial = update(
            50,
            message(CHAT_A, 1, "Новый запрос", date=int(now.timestamp())),
        )
        with sessions.begin() as db:
            report = ingest_updates(
                db,
                [initial],
                bot_id="event-analysis-bot",
                now=now,
                auto_analysis_enabled=True,
                analysis_debounce_seconds=10,
            )
        assert report.analysis_runs_created == 1

        with sessions() as db:
            [first_run] = list(db.scalars(select(AnalysisRun)))
            [outbox] = list(db.scalars(select(OutboxEvent)))
            first_run_id = first_run.id
            assert outbox.aggregate_id == first_run.id
            assert as_utc(outbox.next_attempt_at) >= now + timedelta(seconds=10)

        edited_at = now + timedelta(seconds=5)
        edited = update(
            51,
            {
                **message(
                    CHAT_A, 1, "Новый запрос, уточнение", date=int(now.timestamp())
                ),
                "edit_date": int(edited_at.timestamp()),
            },
            kind="edited_message",
        )
        with sessions.begin() as db:
            report = ingest_updates(
                db,
                [edited],
                bot_id="event-analysis-bot",
                now=edited_at,
                auto_analysis_enabled=True,
                analysis_debounce_seconds=10,
            )
        assert report.analysis_runs_merged == 1

        with sessions() as db:
            runs = list(db.scalars(select(AnalysisRun)))
            events = list(db.scalars(select(OutboxEvent)))
            assert len(runs) == len(events) == 1
            assert runs[0].id == first_run_id
            assert as_utc(runs[0].as_of) == edited_at + timedelta(microseconds=1)
            assert as_utc(runs[0].input_received_at) == edited_at
            assert as_utc(events[0].next_attempt_at) >= edited_at + timedelta(
                seconds=10
            )

        with sessions.begin() as db:
            replay = ingest_updates(
                db,
                [edited],
                bot_id="event-analysis-bot",
                now=edited_at + timedelta(seconds=6),
                auto_analysis_enabled=True,
                analysis_debounce_seconds=10,
            )
            assert replay.analysis_runs_created == replay.analysis_runs_merged == 0

        prepare_input(sessions, first_run_id, finalize=False)
        with sessions() as db:
            [input_message] = list(
                db.scalars(
                    select(AnalysisInputMessage).where(
                        AnalysisInputMessage.run_id == first_run_id
                    )
                )
            )
            revision = db.get(MessageRevision, input_message.revision_id)
            assert revision.snapshot["text"] == "Новый запрос, уточнение"


def test_event_analysis_is_disabled_by_default(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Event analysis disabled")
        connect(browser, project)
        now = datetime.now(UTC)
        with sessions.begin() as db:
            report = ingest_updates(
                db,
                [
                    update(
                        55,
                        message(
                            CHAT_A,
                            1,
                            "Проверочное сообщение",
                            date=int(now.timestamp()),
                        ),
                    )
                ],
                bot_id="disabled-analysis-bot",
                now=now,
            )
        assert report.analysis_runs_created == 0
        with sessions() as db:
            assert list(db.scalars(select(AnalysisRun))) == []


def test_event_analysis_carries_known_telegram_gaps_into_limitations(
    tmp_path, monkeypatch
):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Analysis with known gap")
        connect(browser, project)
        now = datetime.now(UTC)
        with sessions.begin() as db:
            chat = db.scalar(select(Chat).where(Chat.project_id == project))
            connection = db.get(TelegramConnection, chat.id)
            open_gap(
                db, connection, "retention_expired", now - timedelta(seconds=30), now
            )
            ingest_updates(
                db,
                [
                    update(
                        56,
                        message(
                            CHAT_A, 1, "Новое сообщение", date=int(now.timestamp())
                        ),
                    )
                ],
                bot_id="gap-analysis-bot",
                now=now,
                auto_analysis_enabled=True,
                analysis_debounce_seconds=0,
            )
            run = db.scalar(select(AnalysisRun))
            run_id = run.id

        prepare_input(sessions, run_id, finalize=False)
        with sessions() as db:
            run = db.get(AnalysisRun, run_id)
            assert run.limitations["telegram_connection_status"] == "active"
            [gap] = run.limitations["telegram_gaps"]
            assert gap["reason"] == "retention_expired"
            assert datetime.fromisoformat(gap["start_at"]) <= datetime.fromisoformat(
                gap["end_at"]
            )


def test_dispatcher_serializes_automatic_runs_per_project(tmp_path, monkeypatch):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Serialized events")
        connect(browser, project)
        now = datetime.now(UTC)
        with sessions.begin() as db:
            ingest_updates(
                db,
                [update(60, message(CHAT_A, 1, "Первое", date=int(now.timestamp())))],
                bot_id="serialized-bot",
                now=now,
                auto_analysis_enabled=True,
                analysis_debounce_seconds=0,
            )
            [first_event] = list(db.scalars(select(OutboxEvent)))
            first_event.next_attempt_at = now - timedelta(seconds=1)

        class FakeTemporal:
            def __init__(self):
                self.started = []

            async def start_workflow(self, _workflow, run_id, **_kwargs):
                self.started.append(run_id)
                return SimpleNamespace(first_execution_run_id=f"temporal-{run_id}")

        temporal = FakeTemporal()

        async def connect_temporal():
            return temporal

        assert asyncio.run(dispatch_once(sessions, connect_temporal, "test-queue"))
        assert len(temporal.started) == 1

        later = now + timedelta(seconds=1)
        with sessions.begin() as db:
            ingest_updates(
                db,
                [
                    update(
                        61,
                        {
                            **message(
                                CHAT_A, 1, "Поздняя редакция", date=int(now.timestamp())
                            ),
                            "edit_date": int(later.timestamp()),
                        },
                        kind="edited_message",
                    )
                ],
                bot_id="serialized-bot",
                now=later,
                auto_analysis_enabled=True,
                analysis_debounce_seconds=0,
            )
            first_run = db.scalar(
                select(AnalysisRun).where(AnalysisRun.status == "queued")
            )
            first_run_id = first_run.id
            assert len(list(db.scalars(select(AnalysisRun)))) == 2
            [second_event] = list(
                db.scalars(select(OutboxEvent).where(OutboxEvent.status == "pending"))
            )
            second_event.next_attempt_at = later - timedelta(seconds=1)

        assert not asyncio.run(dispatch_once(sessions, connect_temporal, "test-queue"))
        assert len(temporal.started) == 1
        with sessions.begin() as db:
            first_run = db.get(AnalysisRun, first_run_id)
            first_run.status = "succeeded"
        assert asyncio.run(dispatch_once(sessions, connect_temporal, "test-queue"))
        assert len(temporal.started) == 2


class FakeClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.offsets = []

    def get_updates(self, *, offset=None, timeout=30):
        self.offsets.append(offset)
        return self.batches.pop(0)


def test_poll_once_uses_saved_cursor_and_acknowledges_after_commit(
    tmp_path, monkeypatch
):
    settings, sessions = make_app(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        login(browser)
        project = create_project(browser, "Cursor")
        connect(browser, project)
    client = FakeClient([[update(40, message(CHAT_A, 1, "After restart"))], []])
    with sessions() as db:
        # The test uses a known bot identity; the real loop gets it from getMe.
        assert db.get(TelegramBotState, "cursor-bot") is None
    first = poll_once(sessions, client, bot_id="cursor-bot", timeout=1)
    second = poll_once(sessions, client, bot_id="cursor-bot", timeout=1)
    assert first.messages_created == 1
    assert second.received == 0
    assert client.offsets == [None, 41]
    with sessions() as db:
        state = db.get(TelegramBotState, "cursor-bot")
        assert state.last_update_id == 40
        assert state.last_polled_at is not None
