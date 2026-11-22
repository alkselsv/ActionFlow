# AF-024: Cognee source and lifecycle experiment

Ограниченный воспроизводимый эксперимент для [#23](https://github.com/alkselsv/ActionFlow/issues/23). Он проверяет проекцию синтетических обсуждений в Cognee, привязку найденного контекста к исходным сообщениям и жизненный цикл памяти. Это не production-интеграция Cognee и не сравнение качества детекторов.

## Состав

- `corpus.json` — синтетические проекты `alpha`, `beta` и служебный `lifecycle`; реальных переписок нет.
- `projection.py` — чистые правила выбора редакции на дату, формирования `DataItem` и разбора происхождения результата. Cognee здесь не импортируется.
- `run_experiment.py` — запускает Cognee и пишет `results/af024-run.json`.
- `tests/test_projection.py` — проверки срезов, редакций, удаления, изоляции и привязки без загрузки моделей.

Полные результаты и решение: [docs/experiment/af-024-cognee-context.md](../../docs/experiment/af-024-cognee-context.md).

## Запуск

Нужен Python 3.11–3.14. Модели скачиваются при первом запуске; для GLiNER нужно около 750 МБ, для эмбеддингов — около 67 МБ. Запуск из корня репозитория:

```powershell
cd experiments/cognee_context
uv sync
uv run python run_experiment.py --output .runs/af024 --results results/af024-run.json
uv run pytest
```

Обычный запуск использует локальные `fastembed` и `gliner_demo`, без LLM. `Cognee 1.6.10` включён в `pyproject.toml` с extra `gliner`. Результаты эксперимента лежат в `results/af024-run.json`; корень `.runs/` игнорируется Git.

В среде с TLS-инспекцией Python может не доверять сертификату Hugging Face. Тогда установите `truststore` (он уже в dev-группе) и задайте:

```powershell
$env:COGNEE_USE_SYSTEM_CERTS = '1'
uv run python run_experiment.py
```

`COGNEE_USE_SYSTEM_CERTS` не является требованием Cognee; это локальная настройка для проверенной машины.

## Важные ограничения

- В `ENABLE_BACKEND_ACCESS_CONTROL=false` Cognee 1.6.10 не соблюдает `datasets=[...]` и ищет по всем наборам. Эксперимент поэтому использует режим доступа по умолчанию с default user. Это необходимо для изоляции проектов.
- Дата в тексте запроса не является фильтром. Историческая корректность достигается только отдельным набором на каждую пару «проект — as_of».
- `cognee.update()` обновляет текст, но оставляет `external_metadata` старой редакции. Для рабочей проекции нужно пересоздавать элемент, а не полагаться на update.
- `SUMMARIES` не возвращает `external_metadata`; источник восстанавливается через `source_chunk_id` и предшествующий `CHUNKS`.
- `GRAPH_COMPLETION` без LLM-ключа завершается `LLMAPIKeyNotSetError`; в этом прогоне graph completion не проверялся.
