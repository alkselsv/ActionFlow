# AF-004: стартовая конфигурация стека

Дата: 2026-11-21. Статус: часть 2 AF-004, [issue #4](https://github.com/alkselsv/ActionFlow/issues/4). Связанные документы: [политика окружения](af-004-environment-policy.md), [спецификация](../specs/responseflow-mvp.md), [прототип](../../PROTOTYPE.md).

## Решение

Зафиксирован набор версий для локального контура первого эксперимента. Версии проверены на Windows x64 с Docker 29.9.3 и Docker Compose v5.7.0; это не заявление о готовности приложения.

| Компонент | Версия | Решение |
| --- | --- | --- |
| Python | 3.14.10 | Последняя стабильная ветка 3.14; поддерживается FastAPI, Pydantic, SQLAlchemy, Alembic, psycopg и Temporal SDK. 3.15 слишком новая для стартовой конфигурации. |
| uv | 0.13.9 | Управление Python, виртуальным окружением и lock-файлом. |
| FastAPI | 0.165.0 | HTTP API; требует Python >=3.11, совместим с Pydantic 2 и Starlette 1.10. |
| Starlette | 1.10.0 | Транзитивная зависимость FastAPI; зафиксирована lock-файлом. |
| Pydantic | 2.14.4 | Схемы запросов, ответов и структурированного результата. |
| pydantic-settings | 2.16.3 | Конфигурация из окружения и локального `.env`. |
| SQLAlchemy | 2.1.9 | Синхронный доступ к прикладной БД. |
| Alembic | 1.20.1 | Миграции только прикладной БД. |
| psycopg | 3.3.9 (binary) | Драйвер PostgreSQL для SQLAlchemy и Alembic. |
| PostgreSQL | 16.16 | Основная версия 16, которую Temporal тестирует (16.6); 18/19 Temporal пока не тестирует. |
| Temporal Server | 1.32.2 | Persistence и visibility в PostgreSQL; локальный namespace `default`. |
| Temporal admin-tools | 1.32.2 | Схема Temporal и создание namespace. |
| Temporal UI | 2.58.1 | Локальный просмотр workflow. |
| Temporal Python SDK | 1.36.1 | Workflow, activities и worker. |
| Uvicorn | 0.54.0 | Запуск FastAPI локально. |
| pytest | 9.2.0 | Тесты backend. |
| httpx2 | 2.16.2 | HTTP-клиент для `TestClient` Starlette 1.10; `httpx` в этой связке устарел. |
| Ruff | 0.17.6 | Линтер и форматтер Python. |
| Node.js | 26.13.1 LTS (Lithium) | Активная LTS-ветка; Vite, Vitest и ESLint допускают Node 26. |
| npm | 11.20.0 | Идёт с Node 26.13.1; lock-файл фиксирует дерево. |
| React / React DOM | 19.3.1 | Интерфейс менеджера. |
| TypeScript | 7.0.2 | Проверка типов; стабильный релиз от 2026-08-20. |
| Vite | 8.3.8 | Сборка frontend; требует Node ^20.19 или >=22.12. |
| @vitejs/plugin-react | 6.3.0 | React-плагин для Vite 8. |
| Vitest | 5.1.1 | Тесты frontend; требует Node ^22.12, ^24 или >=26. |
| @types/react / @types/react-dom | 19.3.0 | Типы React 19.3. |
| @types/node | 26.6.6 | Типы Node для конфигурации Vite. |

Почему синхронный SQLAlchemy, а не async: на Windows psycopg в async-режиме не работает с циклом Proactor по умолчанию (`Psycopg cannot use the 'ProactorEventLoop' to run in async mode`). Для одного менеджера и умеренной нагрузки синхронные сессии проще и проверены; FastAPI выполняет синхронные обработчики в пуле потоков, а Temporal-активности — через `ThreadPoolExecutor`. Если позже понадобится async, потребуется явно выбрать `SelectorEventLoop` на Windows и проверить worker.

## Проверка совместимости

Проверки выполнялись во временных каталогах, не в репозитории; код приложения не добавлен.

| Проверка | Команда/результат |
| --- | --- |
| Python и backend-зависимости | `uv venv --python 3.14.10`; импорт FastAPI 0.165.0, Pydantic 2.14.4, SQLAlchemy 2.1.9, Alembic 1.20.1, psycopg 3.3.9, Temporal SDK 1.36.1, Uvicorn 0.54.0 — успешно. |
| Фиксация зависимостей | `uv lock` — 39 пакетов; `uv sync --frozen --python 3.14.10` — успешно. |
| FastAPI + Pydantic + TestClient | `pytest` — 1 тест `/health` прошёл; `httpx2` используется без предупреждения об устаревшем `httpx`. |
| Uvicorn | `uvicorn api_app:app --port 58000` → `GET /health` вернул `{"status":"ok"}`. |
| PostgreSQL | `postgres:16.16`, `SELECT version()` → `PostgreSQL 16.16`. |
| SQLAlchemy + psycopg | Синхронный `create_engine("postgresql+psycopg://...")`: insert/select прошли. |
| Alembic | `alembic upgrade head` создал `smoke` и `alembic_version` (`0001`). |
| Temporal + PostgreSQL | `temporalio/server:1.32.2` на PostgreSQL 16.16: `cluster health` → `SERVING`, namespace `default` создан. |
| Temporal Python SDK | Workflow + activity на SDK 1.36.1: результат `Hello, ActionFlow!`. |
| Temporal activity + БД | Синхронная activity через `ThreadPoolExecutor` прочитала `PostgreSQL 16.16`. |
| Temporal UI | `temporalio/ui:2.58.1` → HTTP 200. |
| Node/npm | Node 26.13.1, npm 11.20.0. |
| Frontend build | `tsc --noEmit` (TypeScript 7.0.2) и `vite build` — успешно. |
| Frontend tests | `vitest run` — 2 теста прошли. |
| Docker-образы | Теги `postgres:16.16`, `temporalio/server:1.32.2`, `temporalio/admin-tools:1.32.2`, `temporalio/ui:2.58.1` существуют и запускаются. |

Не проверялось: production-развёртывание, отказоустойчивость, резервное копирование под нагрузкой, вызов выбранного LLM-провайдера и embeddings. Async SQLAlchemy в стартовую конфигурацию не входит: на Windows psycopg требует SelectorEventLoop вместо Proactor; это отдельная проверка, если async понадобится.

## Фиксация зависимостей и конфигурации

- Backend: `pyproject.toml` с точными версиями, `uv.lock` в Git, Python через `.python-version` (`3.14.10`). Установка: `uv sync --frozen`. Транзитивные версии фиксирует lock-файл.
- Frontend: `package.json` с точными версиями без `^`, `package-lock.json` в Git, `.node-version`/`.nvmrc` (`26.13.1`), поле `engines`. Установка: `npm ci`.
- Контейнеры: точные теги образов, без `latest`; обновление — отдельным изменением с повторной проверкой.
- Конфигурация: pydantic-settings читает переменные окружения и локальный `.env`; `.env` уже исключён `.gitignore`. В репозитории допускается `.env.example` только с именами переменных.
- Миграции: Alembic, только прикладная БД; `alembic upgrade head` выполняется явной командой, а не автоматически при старте приложения.

## Локальный контур

| Сервис | Адрес | Назначение |
| --- | --- | --- |
| PostgreSQL 16.16 | `localhost:5432` | Прикладная БД и два хранилища Temporal |
| Temporal Server 1.32.2 | `localhost:7233` | Workflow и activities |
| Temporal UI 2.58.1 | `localhost:8080` | Просмотр запусков |
| Backend (FastAPI) | `127.0.0.1:8000` | API, доступен только локально |
| Frontend (Vite) | `127.0.0.1:5173` | Интерфейс менеджера |

Порты можно переопределить в `.env`, если они заняты. Namespace — `default`, task queue — `actionflow`.

Целевой порядок запуска (команды появятся в #8; приложение ещё не реализовано):

1. `docker compose --env-file .env -f infra/compose.yml up -d postgres`
2. Одноразовый job `temporalio/admin-tools:1.32.2` создаёт БД `temporal`, `temporal_visibility` и применяет схему.
3. `docker compose --env-file .env -f infra/compose.yml -f infra/temporal.compose.yml up -d temporal temporal-ui`
4. Одноразовый job создаёт namespace `default`.
5. `uv sync --frozen && uv run alembic upgrade head`
6. `uv run uvicorn ... --host 127.0.0.1`
7. `uv run python -m actionflow.worker`
8. `npm ci && npm run dev`

Особенности Temporal 1.32.2: образ `server` требует смонтированный dynamic config (например, `development-sql.yaml`), иначе падает на `/etc/temporal/config/dynamicconfig/docker.yaml`; после admin-tools сервер запускается с `SKIP_SCHEMA_SETUP=true`. Эти детали взяты из официального `samples-server` и проверены локально.

## Разделение баз

Один локальный кластер PostgreSQL, разные базы и роли:

- `actionflow` — прикладные данные и решения менеджера; владелец `actionflow`, сюда применяет миграции Alembic.
- `temporal` — persistence Temporal; владелец `temporal`.
- `temporal_visibility` — visibility Temporal; владелец `temporal`.

Роль `actionflow` не имеет доступа к базам Temporal; роль `temporal` не имеет доступа к прикладной БД. Схему Temporal создаёт и обновляет только `admin-tools`, не Alembic. Для пилота допустим один кластер; разделение на отдельные серверы остаётся возможным без изменения контрактов.

## Worker

- Отдельный процесс, не внутри HTTP-сервера.
- Подключается к `TEMPORAL_ADDRESS`, namespace `default`, task queue `actionflow`.
- Синхронные activities выполняются через `ThreadPoolExecutor`; размер пула задаётся конфигурацией и будет ограничен в #7/#12.
- Работа с БД и внешними вызовами — только в activities; workflow не ходит в БД и не выполняет сетевые вызовы.
- Повторные доставки и согласование записи приложения со стартом workflow проектируются в #7/#18, не здесь.
- Перезапуск worker не должен терять workflow; проверка восстановления входит в #12.

## Доступ менеджера

- Backend слушает `127.0.0.1`, не `0.0.0.0`; публикация в интернет не настраивается.
- Одна учётная запись; вход и сессии реализуются в #8.
- CORS разрешает только локальный frontend (`http://127.0.0.1:5173`).
- Удалённый доступ, несколько пользователей и роли не входят в этап.

## Секреты

- `.env` и ключи не попадают в Git, Issues, fixtures и логи.
- Локально — `.env` вне Git или хранилище секретов ОС; в репозитории только `.env.example` с именами.
- Ключи LLM/embedding-провайдера создаются владельцем проекта и передаются через локальный `.env` или хранилище секретов ОС; провайдер по умолчанию не выбран.
- Пароли PostgreSQL в примерах — только фиктивные; реальные значения задаются локально.

## Диагностика

- `/health` — живость процесса; `/ready` — проверка БД и Temporal (появится в #8).
- Структурированные логи с `run_id`/Temporal `workflow_id`; текст переписки и секреты в логи по умолчанию не пишутся.
- Temporal UI на `localhost:8080` для истории workflow; `docker compose logs` для контейнеров.
- Ошибки провайдера, таймауты и повторные попытки будут видны по идентификатору запуска (#12/#18).

## Резервное копирование и восстановление

- Для синтетического эксперимента копии создаются вручную скриптами `scripts/backup.ps1` и проверяются `scripts/verify-backup.ps1`; дампы хранятся вне Git в локальном `backups/`.
- В согласованную копию входят прикладная БД и базы Temporal `temporal` и `temporal_visibility`. Восстановление прикладной БД и Temporal проверено в #19; подробный runbook, RPO/RTO и удаление данных: [af-021-operations.md](af-021-operations.md).
- Ответственный за восстановление — владелец проекта. Для реальных данных потребуется отдельное согласование места, срока хранения и доступа.

## LLM

Согласно [политике окружения](af-004-environment-policy.md) внешние LLM/embedding-провайдеры разрешены. Конкретный провайдер, модель и бюджет по умолчанию не выбраны; конфигурация задаётся в локальном `.env` (шаблон — `.env.example`). Секреты остаются вне Git.

В #13 реализован заменяемый адаптер и локальная детерминированная baseline-модель `local-baseline-unanswered-v1` для типа `unanswered`: она не вызывает внешний API, не передаёт текст наружу и сохраняет `cost_usd = null`. Это позволяет проверить контракт, валидацию оснований, Temporal и карточку. При заполненных `OPENAI_API_KEY`, `OPENAI_MODEL` и `OPENAI_BASE_URL` (или override `ACTIONFLOW_LLM_*`) можно подключить внешний адаптер; выбор качества — в M2.

Контракт: адаптер с структурированным выводом, проверкой схемы и учётом `provider`, `model`, версий prompt/схемы, токенов, стоимости и `run_id`; неизвестная стоимость — `null`, не `0`. Переменные бюджета и тарифов перечислены в `.env.example`.

## Что не сделано

- Нет production-развёртывания, мониторинга, резервного копирования под нагрузкой.
- Не проверены внешняя LLM, embeddings, async SQLAlchemy и Elasticsearch/advanced visibility.
- Линтер frontend не настраивался; версия ESLint выбирается в #8 вместе с конфигурацией.

## Источники

- Python 3.14.10 — https://www.python.org/downloads/
- uv 0.13.9 — https://github.com/astral-sh/uv/releases
- FastAPI 0.165.0 — https://pypi.org/project/fastapi/0.165.0/
- Pydantic 2.14.4 — https://pypi.org/project/pydantic/2.14.4/
- SQLAlchemy 2.1.9 — https://pypi.org/project/SQLAlchemy/2.1.9/
- Alembic 1.20.1 — https://pypi.org/project/alembic/1.20.1/
- psycopg 3.3.9 — https://pypi.org/project/psycopg/3.3.9/
- Temporal Python SDK 1.36.1 — https://github.com/temporalio/sdk-python/releases/tag/1.36.1
- Temporal Server 1.32.2 — https://github.com/temporalio/temporal/releases/tag/v1.32.2
- Temporal: поддерживаемые БД — https://docs.temporal.io/self-hosted-guide/persistence
- Temporal samples-server (PostgreSQL compose и schema setup) — https://github.com/temporalio/samples-server/tree/main/compose
- PostgreSQL: поддерживаемые версии — https://www.postgresql.org/support/versioning/
- Node.js 26.13.1 — https://nodejs.org/en/about/previous-releases
- React 19.3.1 — https://www.npmjs.com/package/react
- TypeScript 7.0.2 — https://github.com/microsoft/TypeScript/releases/tag/v7.0.2
- Vite 8.3.8 — https://www.npmjs.com/package/vite
- Vitest 5.1.1 — https://www.npmjs.com/package/vitest
