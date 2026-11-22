# ResponseFlow — контракты и исполнение анализа

AF-003, часть 2, [#7](https://github.com/alkselsv/ActionFlow/issues/7). Статус: технический проект, не реализация. Опирается на [предметную модель](responseflow-domain-model.md), [правила AF-002](../experiment/af-002-annotation.md), [формат импорта AF-001](../experiment/af-001-import-format.md) и [стек AF-004](../experiment/af-004-stack.md).

Документ фиксирует HTTP-контракты, схему хранения, границы транзакций и исполнение через Temporal. Часть контрактов реализована в M1; документ описывает целевой контракт, а не выдаёт будущие детекторы и действия за готовые.

Реализация #13 следует этим контрактам для типа `unanswered`: используются `candidate`, `candidate_evidence`, `finding`, `finding_observation` и версии детектора в `analysis_run`. Вместо внешнего LLM подключён локальный адаптер `local-baseline-unanswered-v1`; это не меняет схему результата, Activity и правила проверки оснований.

## Принципы

- Модульный монолит: один backend, отдельные процессы API, Temporal worker и outbox dispatcher. Celery, APScheduler и Redis не добавляются.
- PostgreSQL хранит прикладные данные; базы Temporal отдельные. Alembic управляет только прикладной схемой.
- Синхронный SQLAlchemy и psycopg; activities, работающие с БД, синхронные и выполняются через `ThreadPoolExecutor`. Workflow не ходит в БД и не делает сетевых вызовов.
- LLM — заменяемый адаптер. Контракты не зависят от провайдера и модели; внешний вызов выполняется только при явно настроенных провайдере, модели и ключах, иначе используется локальный baseline. Конфигурация хранится вне Git.
- Предметные правила (SLA, приоритет, состояния, идентичность) — чистые функции и сервисы домена, а не prompt. Модель предлагает кандидатов; приложение проверяет и применяет правила.
- Одна учётная запись менеджера, локальный контур, только синтетические данные.

## Структура репозитория

```text
backend/
  pyproject.toml
  uv.lock
  .python-version
  alembic.ini
  migrations/
  src/actionflow/
    api/            # FastAPI app, routers, dependencies, error handlers
    application/    # сценарии: проекты, импорт, запуски, карточки, действия
    domain/         # сущности, enum-ы, правила SLA/приоритета/жизненного цикла
    persistence/    # SQLAlchemy tables, repositories, unit of work, outbox
    temporal/       # workflow, activities, worker, client, dispatcher
    detectors/      # контракт результата, валидатор оснований, LLM/fake адаптеры
    context/        # ContextProvider: manual сейчас, Cognee позже
    config.py
    main.py
  tests/
    unit/
    integration/
    contract/
frontend/
  package.json
  package-lock.json
  src/
    api/            # типизированный клиент
    features/       # auth, projects, imports, queue, findings
    components/
infra/
  compose.yml
  temporal/
    dynamicconfig/development-sql.yaml
  scripts/
    setup-postgres.sh
    create-namespace.sh
data/fixtures/telegram/   # синтетические данные, уже в репозитории
docs/
```

Направление зависимостей: `api → application → domain`; `persistence`, `temporal`, `detectors` и `context` реализуют порты application. Домен не импортирует FastAPI, SQLAlchemy, Temporal и SDK провайдера. Конкретные имена модулей могут уточняться в M1 без изменения контрактов.

## Аутентификация и общие правила API

- Префикс `/api/v1`; JSON в `snake_case`; время в UTC ISO 8601 с `Z`.
- Одна учётная запись. `POST /auth/login` проверяет пароль (Argon2id-хеш хранится вне Git), создаёт серверную сессию и ставит cookie `af_session`: `HttpOnly`, `SameSite=Strict`, `Path=/`, срок 12 часов. `POST /auth/logout` удаляет сессию. `GET /auth/me` возвращает текущего пользователя.
- Регистрации, сброса пароля и ролей нет. Пользователь создаётся CLI-командой при настройке #8.
- Все `/api/v1` кроме `/auth/login` и health требуют сессию; иначе `401 unauthenticated`.
- Доступ к чужому проекту/чату/карточке — `404 not_found`, а не `403`, чтобы не раскрывать существование объектов. Отдельных ролей нет; одна учётная запись имеет доступ ко всем проектам.
- Мутации дополнительно проверяют `Origin`/`Referer` локального frontend. CORS разрешён только `http://127.0.0.1:5173`.
- Ошибки:

```json
{
  "error": {
    "code": "stale_finding",
    "message": "Карточка изменилась после просмотра",
    "details": {"current_version": 7},
    "request_id": "uuid"
  }
}
```

- Коды: `400 malformed_request`, `401 unauthenticated`, `404 not_found`, `409 conflict` (`stale_finding`, `idempotency_conflict`, `chat_already_bound`, `invalid_state`), `413 payload_too_large`, `422 validation_failed` / `invalid_transition`, `503 dependency_unavailable`, `500 internal_error`.
- Пагинация: `limit` (по умолчанию 50, максимум 100) и непрозрачный `cursor`; ответ `{items, next_cursor}`. В MVP допустим offset-пагинация внутри репозитория, но контракт курсорный.
- `Idempotency-Key` обязателен для `POST /analyses` и `POST /findings/{id}/actions`, необязателен для импорта. Повтор с тем же ключом и телом возвращает сохранённый ответ с `Idempotent-Replayed: true`; другое тело — `409 idempotency_conflict`. Ключи хранятся 24 часа.
- Health: `GET /health` — живость процесса. В #8 `GET /ready` проверяет PostgreSQL и возвращает `503` при недоступности. Проверка Temporal добавляется вместе с подключением Temporal.

## HTTP-операции

### Проекты, участники и настройки

| Метод и путь | Вход | Результат | Ошибки |
| --- | --- | --- | --- |
| `GET /projects` | — | список проектов с клиентом и числом сообщений | `401` |
| `POST /projects` | `client_name`, `project_name`, `manager_name` | `201` проект | `422` |
| `GET /projects/{id}` | — | проект, чат, ответственный, текущая версия настроек | `404` |
| `PATCH /projects/{id}` | `project_name?`, `manager_name?` | обновлённый проект | `404`, `422` |
| `GET /projects/{id}/participants` | — | участники с `source_author_id`, стороной и версией настроек | `404` |
| `GET /projects/{id}/settings` | — | текущая версия контекста, SLA, календаря и сторон | `404` |
| `POST /projects/{id}/settings` | `context_text`, `agreements`, `manager_name?`, `client_type`, `work_hours`, `timezone`, `first_response_minutes`, `calendar`, `participant_sides` | `201` новая неизменяемая версия | `404`, `422` |
| `GET /projects/{id}/settings/versions` | `limit`, `cursor` | история версий, новые первыми | `404` |
| `GET /projects/{id}/settings/versions/{version_id}` | — | неизменяемая версия этого проекта | `404` |

Для #9 `work_hours` содержит дни недели (1 — понедельник, 7 — воскресенье), один интервал `start`–`end` в формате ЧЧ:ММ. `calendar.closed_dates` — нерабочие даты в формате ГГГГ-ММ-ДД. `timezone` — имя IANA. Профили `north` и `south` в интерфейсе подставляют SLA 60 и 120 рабочих минут из AF-002; проект сохраняет своё выбранное значение. `custom` используется для ручной настройки без профиля.

Стороны `vedita`, `customer`, `unknown` привязываются к `source_author_id` внутри проекта. Привязку можно сохранить до появления чата или участника. Список участников объединяет сохранённые привязки и наблюдённых авторов из чата проекта. У наблюдённого автора `imported=true`, даже если у него нет отображаемого имени. Автор без привязки отображается как `unknown`.

При сохранении интерфейс передаёт `base_version_id` просмотренной версии или `null` для первой версии. Если условия уже изменились, сервер возвращает `409` и сохраняет все имеющиеся версии.

Создание проекта не создаёт чат: чат появляется при первом импорте. Один проект имеет один чат; повторная привязка другого чата — `409 chat_already_bound`. Стороны участников задаются в версии настроек; изменение любой настройки создаёт новую версию, старые запуски продолжают ссылаться на прежнюю.

### Импорт и переписка

| Метод и путь | Вход | Результат | Ошибки |
| --- | --- | --- | --- |
| `POST /projects/{id}/imports` | `multipart/form-data`, поле `file` (Telegram-style JSON) | `201` отчёт импорта | `413`, `422`, `409` |
| `GET /imports/{id}` | — | отчёт и статус импорта | `404` |
| `GET /projects/{id}/imports` | `limit`, `cursor` | история импортов | `404` |

Путь `POST /projects/{id}/messages/import`, введённый в #10 для JSON-тела, поддерживает повторный импорт в #11. Отчёт дополнительно показывает `missing_from_export`, `system_events` и `uncertain_revisions`. Запись, которой нет в очередном файле, остаётся доступной. С тем же `Idempotency-Key` и телом запрос возвращает сохранённый результат в течение 24 часов.

`GET /projects/{id}/messages/{message_id}/revisions` возвращает наблюдаемые редакции с устойчивыми ID. `observed_at` фиксирует первое наблюдение редакции системой, `source_observed_at` — доступное время наблюдения в синтетическом источнике, `edited_at` — известное время редакции. Неизвестные значения возвращаются как `null`. При неясном порядке импорт сохраняет наблюдение с признаком неопределённости и оставляет предыдущую актуальную редакцию.
| `GET /projects/{id}/messages` | `from?`, `to?`, `limit`, `cursor` | сообщения с автором, стороной, временем, текстом, reply и признаками вложений | `404`, `422` |
| `GET /messages/{id}` | `context=20` | сообщение и окружающая переписка | `404` |

Импорт синхронный в рамках HTTP-запроса; лимит MVP — 50 МБ и 200 000 сообщений на файл. `import_run` создаётся отдельной транзакцией, данные импортируются одной транзакцией. Ошибка откатывает данные и отдельной записью переводит `import_run` в `failed`, чтобы отчёт не потерялся вместе с rollback. Отчёт содержит `imported`, `updated`, `revisions_created`, `skipped`, `warnings`, `unresolved_replies`, `attachments_present`, `period_from`, `period_to`.

Правила импорта:

- Чат идентифицируется `(source, external_id)`; сообщение — `(chat_id, external_id)`. Повторная загрузка обновляет наблюдение, но не создаёт дубликат.
- Новая редакция создаётся только при наблюдённом изменении анализируемых полей. `observed_at` — время импорта; исходное время — `sent_at`.
- Системные события сохраняются как события, а не участники. Вложения и голосовые отмечаются, содержимое не анализируется.
- Отсутствие сообщения в новой выгрузке не считается удалением; это предупреждение отчёта.
- Конфликт «чат уже привязан к другому проекту» — `409`; неподдерживаемый формат, BOM/битый JSON и нарушение обязательных полей — `422` с отчётом ошибок.

### Запуски анализа и сравнение

| Метод и путь | Вход | Результат | Ошибки |
| --- | --- | --- | --- |
| `POST /projects/{id}/analyses` | `period_start`, `period_end`, `as_of`, `apply_to_queue?` (по умолчанию `true`), версии настроек опционально | `202` `{run_id, status, workflow_id}` | `409`, `422` |
| `GET /analyses/{id}` | — | статус, этап, вход, версии, счётчики, ошибки, стоимость, длительность | `404` |
| `GET /analyses/{id}/input` | `limit`, `cursor` | сохранённые ID сообщений и редакций, роль и время доступности | `404` |
| `GET /projects/{id}/analyses` | `limit`, `cursor` | история запусков | `404` |
| `GET /projects/{id}/analyses/compare` | `left`, `right` | различия кандидатов, приоритетов, SLA, стоимости и длительности | `404`, `422` |
| `GET /analyses/{id}/candidates` | — | кандидаты запуска и диагностика валидации | `404` |

`period_start < period_end <= as_of`; иначе `422`. Лимит входа — 50 000 сообщений; превышение даёт `422 analysis_input_too_large`. Если версии настроек не переданы, сервер фиксирует последние версии, действующие на `as_of`, и сохраняет их ID. `apply_to_queue=false` — сравнительный запуск: результаты сохраняются, карточки очереди не обновляются. Обычный запуск с историческим `as_of` по-прежнему обновляет очередь: «исторический» здесь означает режим сравнения, а не дату.

Запуск состояния: `pending → queued → running → succeeded | failed`. `pending` — запись создана, outbox ещё не доставлен; `queued` — workflow стартовал; `failed` хранит `error_code`, `retryable`, `stage`. Отмена запуска в MVP не входит.

В #12 успешный запуск заканчивается этапом `input_ready`. Окно полуоткрытое, а сообщения до его начала могут войти как контекст. Срез фиксирует доступные на момент оценки версии и показывает отсутствующие настройки и ограничения истории. Сторона участника сохраняется отдельно от роли `window|context`. Карточки и результаты детекторов появятся в следующих задачах.

Сравнение возвращает различия, а не объявляет качество: метрики precision/recall считаются офлайн-инструментом на размеченном наборе AF-002. Два запуска сравнимы, если совпадают `input_hash` и версии настроек/детектора; иначе ответ содержит `comparable=false` и причины.

### Очередь и карточки

| Метод и путь | Вход | Результат | Ошибки |
| --- | --- | --- | --- |
| `GET /projects/{id}/findings` | `type?`, `status?`, `priority?`, `sla?`, `limit`, `cursor` | карточки очереди | `404`, `422` |
| `GET /projects/{id}/findings/counts` | — | открытые по типам/приоритетам, нарушения SLA | `404` |
| `GET /findings/{id}` | — | карточка, основания, окружение, SLA, предложение, история, `version` | `404` |
| `GET /findings/{id}/history` | `limit`, `cursor` | наблюдения и решения | `404` |
| `POST /findings/{id}/actions` | `action`, `expected_version`, `comment?`, `snooze_until?`, `proposal_id?` | `200` обновлённая карточка | `409`, `422` |

Сортировка очереди: высокий приоритет → срок SLA/обязательства → `updated_at`. Карточка содержит `type`, `status`, `priority` и `priority_reason`, `sla {due_at, state, first_response_at}`, `subject`, `description`, `suggested_action`, `confidence`, `evidence[]`, `history[]`, `current_run_id`, `version`.

Действия:

| `action` | Обязательные поля | Результат | Ограничения |
| --- | --- | --- | --- |
| `confirm` | `expected_version`; `proposal_id` при пересмотре/переоткрытии | `confirmed` | нет активного предложения для отклонённой/закрытой карточки — `422` |
| `reject` | `expected_version`, `comment` | `rejected` | причина обязательна |
| `snooze` | `expected_version`, `snooze_until` в будущем | `snoozed` | не более одного активного срока |
| `close` | `expected_version`, `comment` (основание разрешения) | `closed` | закрытие не равно подтверждению |
| `dismiss_proposal` | `expected_version`, `proposal_id` | статус не меняется | предложение становится `dismissed` |

Четыре действия `confirm`, `reject`, `snooze` и `close` меняют статус карточки. `dismiss_proposal` — обработка предложения системы, а не пятое действие менеджера: статус карточки сохраняется. При несовпадении `expected_version` — `409 stale_finding` с текущей версией; состояние не меняется. Повтор с тем же `Idempotency-Key` не создаёт второе действие. Ответ содержит новую версию карточки и созданную запись истории.

## Воспроизводимый вход анализа

`analysis_run` фиксирует:

- проект, чат, `period_start`, `period_end`, `as_of`, `apply_to_queue`;
- ID версии настроек (контекст, SLA, календарь, стороны), `input_hash`;
- `detector_config`: тип, `detector_version`, `prompt_version`, `schema_version`, `model_id` (nullable), `model_params` (jsonb, nullable), `context_provider_version`;
- `usage`: токены, `cost_usd` (`null`, если неизвестно), `currency`, `pricing_version`;
- `workflow_id`, `temporal_run_id`, тайминги, ошибки, счётчики.

`analysis_input_message` хранит для каждого сообщения выбранную редакцию, `sent_at`, `observed_at` и роль `window|context`. Вход включает сообщения с `sent_at <= as_of`; в окно попадают сообщения периода, более ранние — как контекст. Детекторы создают якоря только в окне, но могут ссылаться на контекст. Это позволяет воспроизвести срез и не терять окружение оснований.

`input_hash` — SHA-256 по отсортированному списку `(message_id, revision_id, sent_at, observed_at, role)` и версии настроек. Одинаковый hash и версии делают запуски сравнимыми; различие фиксируется, а не скрывается.

## Контракт детектора

Результат одного детектора — JSON, проверяемый Pydantic-схемой `detector_result.v1` с `extra=forbid`. Пример:

```json
{
  "schema_version": "detector_result.v1",
  "finding_type": "commitment",
  "candidates": [
    {
      "candidate_key": "sha256:...",
      "subject": {
        "kind": "commitment",
        "anchor_message_id": "uuid",
        "anchor_revision_id": "uuid",
        "fragment": "пришлю завтра до 12:00",
        "discriminator": "estimate-deadline"
      },
      "description": "Обещание прислать исправленную смету",
      "confidence": 0.82,
      "priority_hint": "low",
      "suggested_action": "follow_up",
      "evidence": [
        {
          "role": "anchor",
          "message_id": "uuid",
          "revision_id": "uuid",
          "quote": "Исправленную смету пришлю завтра до 12:00."
        }
      ],
      "limitations": []
    }
  ],
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 180,
    "cost_usd": null
  },
  "diagnostics": []
}
```

Правила:

- `candidate_key` вычисляется детектором детерминированно для одного результата; при повторной доставке activity тот же ключ не создаёт дубль. Кросс-запусковая идентичность карточки определяется моделью #6 по типу, якорю и смыслу, а не этим ключом: приложение разрешает `subject` в обращение, обязательство или эпизод претензии; неоднозначное сопоставление сохраняется для ручной проверки без автоматического дубля.
- `subject.anchor_message_id` и все `evidence[].message_id` обязаны входить в `analysis_input_message` этого запуска; `revision_id` — совпадать с выбранной редакцией.
- `quote` обязателен для текстового основания и должен быть подстрокой редакции после нормализации пробелов; для медиа допускается `quote: null` и `limitation: "unparsed_content"`.
- Минимум одно основание с ролью `anchor`. Ответ клиента, системное событие и цитата не могут быть основанием реакции Vedita без известной стороны.
- `priority_hint` — только подсказка; итоговый приоритет вычисляет домен по AF-002. `confidence` не считается вероятностью правильности.
- `limitations` кодирует ограничения данных: `missing_text`, `unparsed_content`, `unknown_side`, `ambiguous_reference`, `outside_window`.
- Кандидат с невалидными основаниями сохраняется как `invalid` с ошибками, но не создаёт карточку; запуск завершается `succeeded` с предупреждением. Это сохраняет диагностику и не маскирует ошибку модели.
- Схема и prompt версионируются; неизвестные поля запрещены, новая версия схемы вводится явно.

Адаптер: `DetectorModel.detect(request) -> DetectorResponse` (синхронный вызов внутри activity). Возвращает структурированный результат и usage. Ошибки: `ProviderUnavailable`, `RateLimited`, `Timeout`, `InvalidOutput`; первые три ретраятся, `InvalidOutput` — нет. Без утверждённого провайдера реализация — `UnconfiguredDetector`, которая падает с `LLMNotConfigured`; тесты используют `FakeDetector` и записанные ответы.

## Temporal: workflow и Activities

Workflow `AnalysisWorkflow(run_id)`; ID `analysis:{run_id}`; namespace `default`; task queue `actionflow`; `WorkflowIDReusePolicy.REJECT_DUPLICATE`. Workflow детерминирован и не обращается к БД/сети.

| Шаг | Activity | Запись | Таймаут / повторы |
| --- | --- | --- | --- |
| 1 | `prepare_analysis_input` | фиксирует вход, `input_hash`, статус `running` | 5 мин, 3 попытки |
| 2 | `detect_unanswered` | кандидаты и основания | 5 мин, 4 попытки; LLM-вызов ≤60 с |
| 3 | `detect_commitment` | кандидаты и основания | то же |
| 4 | `detect_complaint` | кандидаты и основания | то же |
| 5 | `apply_rules_and_persist` | приоритет, SLA, карточки, наблюдения | 2 мин, 3 попытки |
| 6 | `finalize_analysis` | статус, счётчики, usage, длительность | 1 мин, 3 попытки |
| — | `record_analysis_failure` | `failed`, ошибка и этап | 1 мин, 2 попытки |

Детекторы выполняются последовательно в MVP: предсказуемая стоимость и меньше конкуренции за провайдера. Параллельный запуск — возможное уточнение без изменения контракта.

**Реализация #13:** Activity `detect_unanswered` вызывает адаптер, проверяет схему и основания и сохраняет кандидата, карточку и наблюдение в одной транзакции. `apply_rules_and_persist` для SLA/приоритета появится в #14; `finalize_analysis` уже переводит запуск в `succeeded` со стадией `unanswered_ready`. `fail_analysis` фиксирует постоянную ошибку и стадию запуска.

Политика повторов: initial 1 с, backoff 2, максимум 5 попыток, максимальный интервал 1 мин. `InvalidOutput`, ошибки валидации, `401/403` и нарушения схемы — non-retryable. `429`, `5xx`, таймауты — retryable. Превышение попыток переводит запуск в `failed`; повторный анализ — новый запуск с новым ID, а не правка старого.

Перезапуск worker: workflow и история хранятся в Temporal, activities идемпотентны по `(run_id, candidate_key)` и уникальным ограничениям; после рестарта выполнение продолжается. `analysis_input_message` уже зафиксирован, поэтому вход не меняется.

## Outbox и границы транзакций

Запуск не стартует workflow прямо из HTTP-запроса. В одной транзакции прикладной БД создаются:

1. `analysis_run` со статусом `pending` и всеми версиями;
2. `outbox_event(kind='start_analysis', aggregate_id=run_id, event_key=run_id)`.

После commit процесс `dispatcher` выбирает события `FOR UPDATE SKIP LOCKED`, вызывает Temporal `start_workflow`, переводит `analysis_run` в `queued` и помечает событие `dispatched`. Повторный вызов с тем же workflow ID даёт `AlreadyStarted`; dispatcher сверяет запуск и считает это успехом. Сбой между записью и стартом не теряет запрос: событие остаётся `pending`/`retrying`. После исчерпания попыток запуск получает `failed` с `stage=dispatch`.

Dispatcher — отдельный процесс с циклом PostgreSQL, без Celery/APScheduler. Интервал опроса 1 с, backoff 1→2→4…→60 с, максимум 10 попыток, `last_error` и `next_attempt_at`. Статусы события: `pending`, `retrying`, `dispatched`, `failed`.

Транзакции:

- **Импорт:** `import_run` создаётся отдельной транзакцией; данные импортируются одной транзакцией. При ошибке данные откатываются, а `import_run` отдельной записью переводится в `failed`; отчёт не смешивается с частичными данными.
- **Вход анализа:** activity фиксирует `analysis_input_message` и `input_hash` одной транзакцией; повтор идемпотентен.
- **Детектор:** внешний вызов вне транзакции; результат, основания и usage пишутся одной транзакцией. Ошибка валидации сохраняет `invalid`-кандидата и диагностику.
- **Карточки:** upsert по `(project_id, finding_type, subject_kind, subject_id)` под `SELECT ... FOR UPDATE` или `ON CONFLICT`; добавляется наблюдение, предложение и основания. Статус и решения менеджера не перезаписываются. Более старый `as_of` не заменяет более свежее наблюдение.
- **Действие менеджера:** `SELECT ... FOR UPDATE` карточки, проверка `version`, проверка перехода и предложения, вставка `manager_action`, обновление карточки, `version += 1`, всё одной транзакцией. Конфликт — `409`, без частичных изменений.
- **Финализация:** статус, счётчики, стоимость и длительность одной транзакцией; повтор не дублирует метрики.

## Схема хранения

Типы: `uuid`, `text`, `timestamptz`, `integer`, `bigint`, `numeric(12,6)`, `jsonb`. Enum-ы — `text` + `CHECK` для простых миграций.

| Таблица | Ключевые поля и ограничения |
| --- | --- |
| `app_user` | `id`, `login unique`, `password_hash`, `created_at` |
| `session` | `id`, `user_id`, `token_hash unique`, `expires_at`, `last_used_at` |
| `client` | `id`, `name` |
| `project` | `id`, `client_id`, `name`, `manager_name`, `created_at` |
| `chat` | `id`, `project_id unique`, `source`, `external_id`, `name`, `type`; `unique(source, external_id)` |
| `participant` | `id`, `chat_id`, `source_author_id`, `display_name`; `unique(chat_id, source_author_id)` |
| `settings_version` | `id`, `project_id`, `version_no`, `context_text`, `agreements`, `manager_name`, `client_type`, `work_hours jsonb`, `timezone`, `first_response_minutes`, `calendar jsonb`, `created_at`; `unique(project_id, version_no)` |
| `participant_side` | `settings_version_id`, `source_author_id`, `side`; PK по версии и внешнему ID автора, включая привязки до импорта |
| `message` | `id`, `chat_id`, `external_id`, `author_participant_id?`, `source_type`, `sent_at`, `reply_to_message_id?`; `unique(chat_id, external_id)` |
| `message_revision` | `id`, `message_id`, `revision_no`, `text?`, `text_format jsonb`, `edited_at?`, `observed_at`, `source_payload jsonb`; `unique(message_id, revision_no)` |
| `message_attachment` | `id`, `revision_id`, `kind`, `media_type`, `mime_type`, `duration_seconds`, `metadata jsonb` |
| `service_event` | `id`, `message_id`, `actor_participant_id?`, `action`, `payload jsonb` |
| `issue` | `id`, `project_id`, `anchor_message_id`, `anchor_revision_id`, `status`, `created_at` |
| `issue_event` | `id`, `issue_id`, `kind`, `message_id`, `revision_id`, `at`, `uncertain` |
| `commitment` | `id`, `project_id`, `issue_id?`, `anchor_message_id`, `anchor_revision_id`, `subject_text`, `assignee_participant_id?`, `status` |
| `commitment_event` | `id`, `commitment_id`, `kind`, `message_id?`, `revision_id?`, `due_at?`, `at`, `uncertain` |
| `complaint_episode` | `id`, `project_id`, `anchor_message_id`, `anchor_revision_id`, `status` |
| `finding` | `id`, `project_id`, `finding_type`, `subject_kind`, `subject_id`, `status`, `priority`, `priority_reason jsonb`, `sla_due_at?`, `sla_state`, `current_run_id`, `version`, `updated_at`; `unique(project_id, finding_type, subject_kind, subject_id)` |
| `finding_observation` | `id`, `finding_id`, `run_id`, `candidate_id`, `summary`, `observed_at` |
| `finding_proposal` | `id`, `finding_id`, `run_id`, `kind`, `status`, `created_at`, `resolved_at?` |
| `candidate` | `id`, `run_id`, `finding_type`, `candidate_key`, `subject_kind`, `anchor_message_id`, `description`, `confidence`, `priority_hint`, `suggested_action`, `limitations jsonb`, `status`, `validation_errors jsonb`, `raw_payload jsonb`; `unique(run_id, candidate_key)` |
| `candidate_evidence` | `id`, `candidate_id`, `role`, `message_id`, `revision_id`, `quote?`, `quote_hash?`; `unique(candidate_id, role, message_id, revision_id, quote_hash)` |
| `analysis_run` | `id`, `project_id`, `status`, `apply_to_queue`, `period_start/end`, `as_of`, `settings_version_id`, `input_hash`, `detector_config jsonb`, `usage jsonb`, `cost_usd?`, `workflow_id`, `temporal_run_id?`, `error_code?`, `retryable?`, `stage?`, `created_at`, `started_at?`, `finished_at?`, `idempotency_key`; `unique(project_id, idempotency_key)` |
| `analysis_input_message` | `run_id`, `message_id`, `revision_id`, `sent_at`, `observed_at`, `role`; `unique(run_id, message_id)` |
| `analysis_error` | `id`, `run_id`, `stage`, `code`, `message`, `retryable`, `details jsonb`, `created_at` |
| `manager_action` | `id`, `finding_id`, `action`, `from_status`, `to_status`, `comment?`, `proposal_id?`, `expected_version`, `actor_user_id`, `idempotency_key unique`, `created_at` |
| `import_run` | `id`, `project_id`, `status`, `file_name`, `file_size`, `report jsonb`, `error jsonb?`, `created_at`, `finished_at?` |
| `outbox_event` | `id`, `event_key unique`, `kind`, `aggregate_type`, `aggregate_id`, `payload jsonb`, `status`, `attempts`, `next_attempt_at`, `last_error?`, `dispatched_at?` |
| `idempotency_key` | `id`, `user_id`, `endpoint`, `key`, `request_hash`, `response_status`, `response_body jsonb`, `expires_at`; `unique(user_id, endpoint, key)` |

Индексы: очередь `(project_id, status, priority, updated_at DESC)`, `(project_id, finding_type, status)`, `(finding_id, created_at DESC)` для истории; `analysis_run(project_id, created_at DESC)`, `analysis_input_message(run_id)`; `candidate(run_id)`; `outbox_event(status, next_attempt_at)`; `session(expires_at)`. Уникальные ограничения — основной механизм идемпотентности при гонках.

Миграции: Alembic, одна начальная ревизия и далее по одной на изменение; `alembic upgrade head` — явная команда, не автоматически при старте API. Тесты применяют миграции на пустую БД и проверяют откат последней ревизии в dev-контуре. Схема Temporal не входит в Alembic.

## Проверка и восстановление

| Контур | Проверка |
| --- | --- |
| Контракты API | OpenAPI-снимок; коды ошибок; auth/cookie; пагинация; идемпотентность и конфликт тела |
| Импорт | 15 fixtures, повторная загрузка, BOM/битый JSON, составной текст, вложения, системные события, лимит размера, уникальные ограничения |
| Детектор | FakeDetector; схема `detector_result.v1`; ссылки на проект/редакцию/цитату; неизвестная сторона; битое основание; timeout/InvalidOutput/повтор |
| SLA и приоритет | 12 срезов AF-002, границы рабочего времени, `Europe/Moscow`, обещания без срока, просрочка |
| Решения | Четыре действия, предложения переоткрытия/пересмотра, stale `409`, повтор ключа, новые основания после закрытия |
| Temporal | Падение dispatcher между commit и start; повторный start; убийство worker; retry и non-retryable; постоянная ошибка |
| Сравнение | Одинаковый `input_hash`; разные настройки помечены `comparable=false`; решения менеджера не меняются |
| Восстановление | Повтор dispatcher после сбоя; повторный запуск анализа; миграции на пустой БД; восстановление дампа в #19; зависший `running` выявляется по workflow history и переводится в `failed` вручную в #19 |

Логи структурированы и содержат `request_id`, `run_id`, `workflow_id`, этап и код ошибки; текст переписки и секреты не логируются. `/ready` проверяет БД и Temporal; Temporal UI доступен локально.

## Ограничения и открытые вопросы

- LLM-провайдер и модель по умолчанию не выбраны; внешний вызов включается только заполненной конфигурацией из `.env`. Контракт допускает `model_id = null` и `cost_usd = null`; неизвестная стоимость не записывается как ноль.
- В MVP нет отмены запуска, WebSocket, фонового импорта, нескольких пользователей и ролей.
- Точные UI-сценарии — в #8–#19; документ фиксирует только API и данные.
- Пороговые значения качества и выбор Cognee остаются в M2.
- Эксплуатационные RPO/RTO и резервное копирование — в #19.
