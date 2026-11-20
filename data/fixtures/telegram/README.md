# Синтетические Telegram-style fixtures

Эти файлы получены из предоставленного пользователем синтетического набора нормализованных JSONL-сообщений. Они предназначены для разработки и проверки последующей нормализации; это не экспорт Telegram и не реальные клиентские переписки.

## Состав

Создано 9 отдельных conversation JSON-файлов (по одному на каждый `workspace_id` + `conversation_id`), всего 43 сообщения:

- `north-conv-n-001.json` — 13 сообщений;
- `north-conv-n-002.json` — 6;
- `north-conv-n-003.json` — 3;
- `north-conv-n-004.json` — 4;
- `north-conv-n-005.json` — 4;
- `north-conv-n-006.json` — 3;
- `north-conv-n-007.json` — 4;
- `north-conv-n-008.json` — 3;
- `south-conv-s-001.json` — 3.

Идентификаторы чатов, отправителей и отображаемые имена с префиксом `Synthetic` искусственные. Роли участников перенесены из исходного набора. Для неизвестного в источнике соответствия «workspace → клиент/проект» такого соответствия не предполагается.

## Перенос полей

| Исходное поле | Telegram-style поле |
| --- | --- |
| `conversation_id` | имя чата и `_actionflow_fixture.conversation_id` |
| `external_id` | `_actionflow_synthetic_source.external_id` |
| `sent_at` | `date`, `date_unixtime` и исходное значение в `_actionflow_synthetic_source.sent_at` |
| `sender_id`, `sender_role` | синтетические `from`, `from_id`; исходная роль — в `_actionflow_synthetic_source.sender_role` |
| `text` | `text` |
| `reply_to_external_id` | локальный `reply_to_message_id` и исходная ссылка в metadata |
| `revision`, `observed_at`, `source` | `_actionflow_synthetic_source` |
| `workspace_id` | `_actionflow_fixture.workspace_id` |

`date` сериализован как UTC-время, а исходное время с offset сохранено в metadata. Номера `id` в чате и `reply_to_message_id` локальны для каждого файла; исходные внешние ID не изменяются.

## Ограничения

- Входной файл уже нормализован и не содержит исходных байтов/структуры Telegram Desktop export. Эти fixtures не подтверждают реальные поля, системные типы событий, вложения, часовой пояс экспорта или поведение Telegram при редактировании/удалении.
- Системные записи источника представлены как synthetic messages; их нельзя считать точным воспроизведением схемы Telegram service events.
- В исходнике есть даты и содержательные сценарии, но нет подтверждения, что они соответствуют выбранным клиенту, проекту, чату и периоду эксперимента. Поэтому их нельзя использовать как доказательство согласованного набора реальных данных.
- В репозитории допустимы только синтетические/обезличенные примеры. Реальные переписки и исходные экспорты следует хранить согласованно вне Git.
