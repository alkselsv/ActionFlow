# AF-021: эксплуатация эксперимента и восстановление

Дата: 2026-11-23. Статус: эксплуатационный контур M1 подготовлен в [#19](https://github.com/alkselsv/ActionFlow/issues/19). Связанные документы: [политика окружения](af-004-environment-policy.md), [стартовая конфигурация](af-004-stack.md), [спецификация MVP](../specs/responseflow-mvp.md), [дорожная карта](../ROADMAP.md).

## 1. Контур и доступ

- Один локальный Docker Compose на машине владельца эксперимента; приложение слушает только `127.0.0.1`.
- Один менеджер. Вход — `POST /api/v1/auth/login`, пароль проверяется Argon2, серверная сессия хранится как hash и передаётся в `HttpOnly`/`SameSite=strict` cookie. Выход удаляет сессию.
- API: `127.0.0.1:8000`; интерфейс: `127.0.0.1:5173`; PostgreSQL: `127.0.0.1:5432`; Temporal: `127.0.0.1:7233`; Temporal UI: `127.0.0.1:8080`.
- Публикация портов в интернет, несколько пользователей, роли и удалённый вход не входят в M1.
- Пароль менеджера вводится в терминале при `python -m actionflow.setup_manager manager`; повторная настройка меняет пароль и завершает старые сессии.

## 2. Данные, секреты и хранение

- В репозитории и эксперименте используются только синтетические fixtures из `data/fixtures/telegram/`.
- Реальные переписки, документы, дампы и секреты запрещено коммитить. `.env`, `backups/` и `data/*` вне fixtures исключены `.gitignore`.
- `README.md` и `.env.example` содержат только имена переменных и фиктивные значения. Реальные пароли и ключи — в локальном `.env` или хранилище секретов ОС.
- Секреты не выводятся в диагностику: `actionflow.ops` и `/diagnostics` сообщают только `key_present`, режим провайдера и имена полей, без значений.
- При появлении реальных данных политика [AF-004](af-004-environment-policy.md) требует отдельного согласования хранения и резервных копий.

## 3. Запуск и конфигурация

Из корня репозитория:

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f infra/compose.yml up -d --wait postgres
uv sync --project backend --frozen
uv run --project backend --frozen alembic -c backend/alembic.ini upgrade head
$env:PYTHONPATH = 'backend/src'
uv run --project backend --frozen python -m actionflow.setup_manager manager
uv run --project backend --frozen uvicorn actionflow.main:app --host 127.0.0.1 --port 8000
```

Во втором терминале:

```powershell
$env:PYTHONPATH = 'backend/src'
uv run --project backend --frozen python -m actionflow.dispatcher
```

В третьем:

```powershell
$env:PYTHONPATH = 'backend/src'
uv run --project backend --frozen python -m actionflow.worker
```

В четвёртом:

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run dev
```

Temporal и UI:

```powershell
docker compose --env-file .env -f infra/compose.yml -f infra/temporal.compose.yml up -d --wait temporal temporal-ui temporal-namespace
```

Порядок важен: PostgreSQL и миграции → API → dispatcher и worker → frontend. Temporal можно поднять до запуска анализа. Перезапуск worker или API не удаляет строки прикладной БД и решения менеджера; workflow продолжает использовать Temporal.

Основные переменные `.env`:

| Переменная | Назначение |
| --- | --- |
| `POSTGRES_*` | прикладная PostgreSQL; пароль не коммитится |
| `TEMPORAL_DB_PASSWORD` | PostgreSQL Temporal |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` | подключение worker/dispatcher |
| `ACTIONFLOW_FRONTEND_ORIGIN` | разрешённый origin и CORS |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | необязательный внешний провайдер |
| `ACTIONFLOW_LLM_*` | таймаут, повторы, бюджет и тарифы |
| `ACTIONFLOW_DATABASE_URL` | необязательный явный URL вместо `POSTGRES_*` |

Без внешнего ключа используется локальный детерминированный baseline; `cost_usd` остаётся `null`, а не `0`.

## 4. Проверки и диагностика

```powershell
.\scripts\check.ps1
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/health` проверяет процесс API, `/ready` — PostgreSQL и Temporal. Ответ `/ready` не требует входа и не содержит переписки.

Диагностика контура и запуска:

```powershell
$env:PYTHONPATH = 'backend/src'
uv run --project backend --frozen python -m actionflow.ops doctor
uv run --project backend --frozen python -m actionflow.ops doctor --probe-provider
uv run --project backend --frozen python -m actionflow.ops run <run_id>
```

`doctor` показывает БД, Temporal, worker, режим провайдера и политику логов; код возврата `1`, если БД, Temporal или worker недоступны. `--probe-provider` делает только необязательный запрос списка моделей к настроенному OpenAI-совместимому провайдеру и не передаёт текст переписки. Для локального baseline сетевой вызов не выполняется.

Те же данные доступны после входа:

- `GET /api/v1/diagnostics` — БД, worker, провайдер, политика логов;
- `GET /api/v1/analyses/{run_id}/diagnostics` — состояние, стадия, `error_code`, `retryable`, workflow ID, Temporal run ID, outbox и worker;
- `GET /api/v1/analyses/{run_id}` — состояние запуска без операционного контекста.

Worker обновляет heartbeat в `worker_heartbeat` каждые 10 секунд. Heartbeat старше 30 секунд считается недоступным; это отличает остановленный worker от недоступной БД. Outbox показывает `attempts` и `last_error`, если dispatcher не может запустить workflow.

Для просмотра workflow откройте Temporal UI `http://127.0.0.1:8080`; workflow ID — `analysis:{run_id}`, task queue — `actionflow`.

## 5. Резервное копирование и восстановление

### Выбранная схема

- Владелец восстановления — владелец проекта.
- Копии создаются вручную: перед миграцией/восстановлением, перед рискованным изменением и в конце рабочей сессии. Автоматическое расписание для синтетического M1 не требуется.
- Хранятся последние три копии в локальном `backups/` вне Git; старые удаляются вручную. Для реальных данных потребуется отдельное согласование места, срока и доступа.
- RPO: `0` на момент согласованной остановленной копии; при копировании на работающем контуре — с момента последней копии.
- RTO для одного локального узла: не более 30 минут.
- Прикладная БД и обе базы Temporal (`temporal`, `temporal_visibility`) входят в согласованную копию.

### Создание копии

Остановите API, worker и dispatcher (Ctrl+C), затем Temporal server. PostgreSQL может оставаться запущенным: `pg_dump` даёт согласованный снимок при отсутствии записи.

```powershell
docker compose --env-file .env -f infra/compose.yml -f infra/temporal.compose.yml stop temporal
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -RequireTemporal -Quiesced
```

Скрипт кладёт `app.dump`, `temporal.dump`, `temporal_visibility.dump` и `manifest.json` с размерами и SHA-256 в `backups/<timestamp>/`. Секреты в manifest не попадают. Если Temporal не нужен, используйте `-SkipTemporal`; для приёмочной проверки лучше `-RequireTemporal`.

### Проверка восстановления

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-backup.ps1 -RequireTemporal
```

Скрипт создаёт новую копию, восстанавливает прикладную БД в отдельную временную базу, проверяет версию Alembic и таблицы решений, восстанавливает Temporal в временные базы, проверяет `schema_version` и удаляет временные базы. Рабочие базы не изменяются.

Проверка 2026-11-23: прикладная БД восстановлена с версией `0010_operations`, `temporal` и `temporal_visibility` — по одной строке `schema_version`; временные базы удалены. Перезапуск API с сохранённой сессией сохранил подтверждённую карточку и `manager_action`.

### Восстановление прикладной БД

1. Остановите API, worker и dispatcher.
2. При необходимости сохраните текущую копию.
3. Восстановите в рабочую базу с `-Force` (или сначала в отдельную базу без `-Force`):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupDir .\backups\<timestamp> -TargetDatabase actionflow -Force
```

4. Запустите API, worker и dispatcher. Решения менеджера, история и `manager_action` восстанавливаются вместе с БД. Redis/очередей приложения нет; состояние исполнения находится в Temporal.

### Восстановление Temporal

Восстановление поверх рабочего контура требует явного подтверждения и останавливает Temporal:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\restore-temporal.ps1 -BackupDir .\backups\<timestamp> -InPlace -Confirm
```

Скрипт останавливает Temporal, восстанавливает `temporal` и `temporal_visibility`, затем запускает сервер; UI можно оставить запущенным или перезапустить отдельно. После восстановления проверьте `/ready`, Temporal UI и повторный запуск анализа. Не запускайте `alembic downgrade` для отката данных: восстанавливайте дамп.

### Перезапуск компонентов

- API/dispatcher/worker можно перезапускать независимо.
- Перезапуск worker не теряет workflow: задача остаётся в Temporal и будет подхвачена worker с тем же task queue.
- Перезапуск API не теряет карточки и решения: они в PostgreSQL.
- Перезапуск PostgreSQL требует корректного `docker compose stop/start postgres`; том `actionflow-data` сохраняется. Не используйте `down -v`, если копия не нужна.

## 6. Удаление экспериментальных данных

Удаление необратимо. Сначала убедитесь, что в контуре нет реальных данных и что нужные синтетические результаты сохранены вне БД.

```powershell
docker compose --env-file .env -f infra/compose.yml -f infra/temporal.compose.yml down -v
Remove-Item -Recurse -Force .\backups
Remove-Item -Force .env
```

`down -v` удаляет тома прикладной БД и Temporal, включая историю решений и execution state. Дополнительно проверьте `docker volume ls` и удалите оставшиеся тома `infra_*`, если они не используются. Синтетические fixtures в Git не удаляются.

## 7. Логи и обращение с перепиской

- API не логирует тело сообщений; ошибки возвращают `request_id`, код и безопасное описание.
- Worker логирует только тип ошибки heartbeat; dispatcher не пишет переписку. Диагностика по запуску использует `run_id`, `workflow_id`, стадию, `error_code` и outbox, а не текст сообщений.
- `actionflow.ops` и `/diagnostics` не возвращают текст сообщений, секреты или содержимое `.env`.
- Temporal workflow получает только `run_id`; activities работают с БД и не передают переписку в Temporal payload.
- При разборе инцидента используйте run ID и карточку в UI; не копируйте реальные сообщения в Issues, логи или отчёты.

## 8. Соответствие AF-004 и границы

Выполнены условия локального контура: один менеджер, localhost, только синтетика, секреты вне Git, внешний провайдер выключен по умолчанию, бюджет и тарифы задаются в `.env`. Резервное копирование и удаление описаны для выбранного локального узла.

Не входят в AF-021: высокая доступность SaaS, несколько компаний, инфраструктура живого бота, автоматическое расписание копий и финальная оценка качества. Полный набор детекторов проверяется в приёмке #20; для эксплуатационного сценария достаточно сквозного `unanswered`, при этом в M1 уже доступны `commitment` и `complaint`.
