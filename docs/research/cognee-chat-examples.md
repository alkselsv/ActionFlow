# Cognee: примеры памяти из переписки

Проверено 2026-11-19 по официальной документации и исходникам `main`; коннекторы и реальные данные не запускались. Точка входа — [llms-core.md](https://docs.cognee.ai/llms-core.md). Это дополнение к [основной заметке](cognee-memory.md). Ссылки на `main` изменяемы; совместимость установленного релиза отдельно не проверена.

## 1. Импорт истории Slack: самый близкий пример

Текущая [Slack integration](https://docs.cognee.ai/integrations/slack-integration#importing-channel-and-thread-history) документирует `/cognee-import`, импорт выбранного треда и HTTP API `POST /api/v1/slack/history/{team_id}/import`. Вход — каналы, ссылки/идентификаторы тредов и период. Тред импортируется целиком, включая ответы вне периода. DMs не поддерживаются.

Один документ соответствует разговору: workspace, канал, корень треда и permalink; внутри — сообщения с ISO timestamp, автором, ссылкой и текстом. Идентичность документа зависит от dataset + workspace + channel + thread root; неизменившийся digest пропускается, изменившийся документ заменяется. Node sets обозначают Slack/workspace/channel. Это сохранение структуры в тексте, а не обещание отдельных узлов `Message` и ребер `reply_to`.

Фоновый sync по умолчанию каждые 6 часов перечитывает разговоры, ловит правки, поздние ответы и подтвержденные удаления. Начальная дата фиксируется: объем чтения растет. Есть лимиты запросов/сообщений. Импорт требует владельца OAuth-подключения и прав read/write/delete на dataset. Slack membership не переносится в ACL: граница доступа — dataset. [Источник всех деталей раздела](https://docs.cognee.ai/integrations/slack-integration).

Также [официальный каталог примеров](https://github.com/topoteretes/cognee/blob/main/examples/README.md) описывает SDK-пример `examples/guides/slack.py`: тред — документ, сообщения без ответов группируются по дню, изменения перерисовывают соответствующий тред/день. Сам скрипт в этой проверке прочитать не удалось; это свидетельство каталога, а не проверка реализации.

## 2. Standalone Slack bot: конкретная реализация и ее ограничения

Это другой путь: [README](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/slack/README.md) использует старые `/add → /cognify → /search`, Socket Mode, opt-in публичных каналов. Редактирования/удаления не синхронизируются, forget очищает канал целиком.

[Обработчик событий](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/slack/cognee_integration_slack/slack_app.py) берет `channel`, `ts`, `user`, `text`, получает permalink; пропускает subtype-события, ботов, пустые сообщения и вопросы с упоминанием самого бота. [ConversationRef](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/slack/cognee_integration_slack/memory_adapter.py) сохраняет `thread_ts` для отправки ответа в тред, но dataset остается `slack_<channel_id>`: thread не становится границей памяти.

[CogneeChatMemory](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/slack/cognee_integration_slack/cognee_memory.py) добавляет к каждому тексту заголовок `[cognee-slack] channel=… ts=… author=… permalink=…`. Это обход ограничения конкретного HTTP-пути: цитирование восстанавливается из текста. Документ добавляется отдельно; `cognify` отложен. Ответ — два поиска: `GRAPH_COMPLETION` для прозы и `CHUNKS` для ссылок. Следовательно, ссылки получены отдельным поиском и не доказывают каждое утверждение ответа. При потере заголовка/ссылки остается текстовая цитата. Явного построения `Message → reply_to → Message` здесь нет.

[IngestionBuffer](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/slack/cognee_integration_slack/ingestion_buffer.py) запускает cognify после 10 сообщений либо перед вопросом. Это пакетный запуск обработки, а не объединение десяти сообщений в один входной документ. Параллельные flush одного канала сериализуются.

## 3. Общий chat-memory adapter и Telegram

[Chat-memory README](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/chat-memory/README.md) содержит исполняемый шаблон: `Conversation(platform="slack", workspace="T1", channel="C1", user="U1")`, затем `ingest(... Message(text="We ship on Friday.", user="U1", permalink=...))` и `answer(..., "when do we ship?")`. Это именно сообщения платформы, а не исключительно диалог assistant/user. Адаптер отделяет durable dataset от session, предлагает per-channel/per-user scope, consent и цитаты permalink+author. HTTP backend использует `/remember | recall | forget`; SDK backend связывает цитаты через provenance/data_id. Согласно README, precise per-user forget в общем dataset требует SDK; HTTP-вариант очищает dataset. Этот контракт нельзя автоматически приписывать старому Slack bot выше.

[Telegram README](https://github.com/topoteretes/cognee-integrations/blob/main/integrations/telegram/README.md) описывает пассивный захват текста и media captions, `/ask`, `/forget`, opt-out. Dataset определяется DM, группой или forum topic: `telegram_dm_<user>` / `telegram_group_<chat>[_<thread>]`. Источники — ссылки на сообщения в supergroup либо цитаты текста. Для связывания evidence с сообщением используется **in-memory ledger последних 1000 сообщений чата**; устойчивость цитирования после рестарта этим README не подтверждена. Timestamp/reply-to как самостоятельные графовые связи README не обещает. Захват по умолчанию включен; чтение группы требует настройки privacy mode/прав бота.

Для Discord найден [issue с предложением коннектора](https://github.com/topoteretes/cognee/issues/4729), где описаны документы channel/thread/day, incremental sync и удаления. Issue само по себе не подтверждает поставку готового коннектора. Проверенного WhatsApp-примера в просмотренных официальных индексах и результатах поиска не найдено; это ограничение поиска, не утверждение об отсутствии поддержки.

## 4. Соседние примеры, проверенные в рамках исследования

- [Persistent Memory Across Sessions](https://docs.cognee.ai/guides/persistent-memory-across-sessions) и [Memory Loop Walkthrough](https://docs.cognee.ai/examples/memory-loop-walkthrough): `session_id` хранит историю Q&A, `improve` переносит знания в граф. Walkthrough использует AUTO_FEEDBACK и дистилляцию каждые три хода; демонстрирует правила/предпочтения, а не импорт произвольной групповой переписки.
- [Follow-Up Agent](https://docs.cognee.ai/cookbooks/follow-up-agent): память Granola + Gmail + Linear, три прохода recall, затем next steps с owner/team/deadline/issue из совокупного контекста. Sample не публикует в Slack; режим с настроенными реальными источниками предусматривает публикацию. Это близкий сценарий извлечения действий из коммуникаций, но не измерение качества такого извлечения.

## Вывод для ActionFlow — предложение, а не результат испытаний

Примеры подтверждают применимость Cognee как слоя поиска контекста по человеческой переписке. Они не подтверждают, что стандартная экстракция надежно сохраняет каждое сообщение, точную последовательность, авторство каждого извлеченного факта и семантику ответов/исправлений. Для проверки нужны реальные ожидаемые вопросы и контрольные ответы.

Предлагаемый адаптер ActionFlow должен сохранять исходный message ID, conversation ID, author ID, timestamp и reply-to в каноническом хранилище, затем передавать связные разговоры в Cognee и возвращать evidence с исходными ID. Slack history importer — более подходящий ориентир для обновляемого архива, standalone bot — для минимального ingest/answer и пакетной обработки. Отдельно проверить фразы «беру на себя», перенос срока и отмену договоренности с последующим ответом в треде.

Если нужны гарантированные структурные связи, [custom data models](https://docs.cognee.ai/guides/custom-data-models) позволяют напрямую записывать типизированные узлы/отношения через `add_data_points`, минуя cognify; скалярный `external_id` сохраняется свойством, embedding определяется `index_fields`. [Custom graph model](https://docs.cognee.ai/guides/custom-graph-model) управляет LLM-экстракцией и identity fields, но typed Edge/FromIdentity требуют SDK, поскольку JSON HTTP теряет эти аннотации. [Typed extraction pipeline](https://docs.cognee.ai/examples/typed-extraction-pipeline) показывает атрибуцию claim к person и `source_hash`. Это ориентиры для собственного слоя provenance; не готовая схема сообщений ActionFlow.
