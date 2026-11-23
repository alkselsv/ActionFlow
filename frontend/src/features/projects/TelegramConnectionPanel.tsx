import { useEffect, useState, type FormEvent } from "react";
import { ApiError, errorMessage, request, type TelegramStatus } from "../../api/client";

const labels = {
  active: "Подключён",
  needs_check: "Требует проверки",
  disconnected: "Отключён",
};
const reasons: Record<string, string> = {
  retention_expired: "Истёк срок хранения очереди Telegram",
  access_lost: "У бота отсутствовал доступ",
  disconnected: "Приём отключён вручную",
  event_unreadable: "Получено событие, которое не удалось обработать",
  rate_limited: "Telegram ограничил частоту запросов",
  access_denied: "Telegram отказал в доступе",
  temporary: "Временная ошибка Telegram",
  configuration: "Требуется проверить настройки коннектора",
  database_unavailable: "База данных временно недоступна",
};
const localTime = (value: string) => new Date(value).toLocaleString();

export function TelegramConnectionPanel({
  projectId,
  onExpired,
  onChanged,
}: {
  projectId: string;
  onExpired: () => void;
  onChanged: () => void;
}) {
  const path = `/projects/${projectId}/telegram`;
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const [notice, setNotice] = useState("");

  async function perform(action: () => Promise<void>) {
    setPending(true);
    setError("");
    try {
      await action();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) onExpired();
      else setError(errorMessage(cause));
    } finally {
      setPending(false);
    }
  }

  async function load() {
    setStatus(await request<TelegramStatus>(path));
    setRefreshError("");
  }

  useEffect(() => { void perform(load); }, [path]);

  useEffect(() => {
    if (pending || !status?.connection) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = () => {
      void request<TelegramStatus>(path).then((next) => {
        if (active) {
          setStatus(next);
          setRefreshError("");
        }
      }).catch((cause) => {
        if (!active) return;
        if (cause instanceof ApiError && cause.status === 401) onExpired();
        else setRefreshError(errorMessage(cause));
      }).finally(() => {
        if (active) timer = setTimeout(refresh, 15000);
      });
    };
    timer = setTimeout(refresh, 15000);
    return () => { active = false; clearTimeout(timer); };
  }, [path, pending, Boolean(status?.connection)]);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setNotice("");
    await perform(async () => {
      const result = await request<TelegramStatus>(path + "/connect", {
        chat_id: data.get("chat_id"),
        name: String(data.get("name") || "").trim() || null,
      });
      setStatus(result);
      setNotice("Чат подключён. Новые сообщения появятся после запуска коннектора.");
      form.reset();
      onChanged();
    });
  }

  async function disconnect() {
    if (!window.confirm("Отключить тестовый Telegram-чат? История сообщений сохранится.")) return;
    setNotice("");
    await perform(async () => {
      setStatus(await request<TelegramStatus>(path + "/disconnect", {}));
      setNotice("Чат отключён; новые события больше не принимаются.");
    });
  }

  const connection = status?.connection ?? null;
  return <section className="panel">
    <h2>Telegram-бот · тестовый чат</h2>
    {pending && <p role="status">Загрузка подключения…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert">{error} <button type="button" className="quiet" disabled={pending} onClick={() => void perform(load)}>Повторить</button></p>}
    {refreshError && <p role="alert">{refreshError}</p>}
    {status && !status.bot_configured && <p>Токен бота не настроен в локальном окружении. Подключение можно сохранить, но коннектор не запустится.</p>}
    {connection ? <article>
      <dl>
        <dt>Чат</dt><dd>{connection.name} · <code>{connection.chat_id}</code></dd>
        <dt>Состояние</dt><dd>{labels[connection.status]}</dd>
        <dt>Последнее событие</dt><dd>{connection.last_event_at ? new Date(connection.last_event_at).toLocaleString() : "Ещё не было"}</dd>
        <dt>Последнее наблюдение чата</dt><dd>{connection.last_observed_at ? localTime(connection.last_observed_at) : "Ещё не было"}</dd>
        <dt>Последний успешный опрос</dt><dd>{connection.last_polled_at ? localTime(connection.last_polled_at) : "Ещё не было"}{connection.polling_delayed && connection.status !== "disconnected" ? " · приём задерживается" : ""}</dd>
        <dt>Последняя выгрузка очереди</dt><dd>{connection.queue_caught_up_at ? localTime(connection.queue_caught_up_at) : "Ещё не было"}</dd>
        <dt>Последняя сверка экспортом</dt><dd>{connection.last_reconciled_at ? localTime(connection.last_reconciled_at) : "Ещё не проводилась"}</dd>
      </dl>
      {connection.polling_error && <p role="alert">
        {reasons[connection.polling_error] ?? "Ошибка опроса"}. Неудачных попыток: {connection.polling_failures}.
        {connection.retry_at && <> Следующая попытка разрешена с {localTime(connection.retry_at)}.</>}
      </p>}
      {connection.last_error && <p role="alert">Требуется проверка источника: {connection.last_error}. Отсутствие новых сообщений не считается доказательством отсутствия ответа.</p>}
      <p>Полноту истории Telegram подтвердить нельзя. Пустой ответ или отсутствие сообщения в экспорте не подтверждают удаление.</p>
      {connection.gaps.length > 0 && <><h3>Известные интервалы неполноты</h3>
        <ul>{connection.gaps.map((gap, index) => <li key={index}>
          {localTime(gap.start_at)} — {gap.end_at ? localTime(gap.end_at) : "продолжается"}. {reasons[gap.reason] ?? gap.reason}.
        </li>)}</ul>
        <p>Для сверки загрузите дополнительный JSON-экспорт этого чата в разделе «Переписка».</p>
      </>}
      <p>Чат привязан вручную по устойчивому ID. Новые текстовые сообщения, ответы, доступные редакции и нетекстовые элементы появятся в переписке после запуска коннектора. Сторону нового участника можно задать в настройках проекта.</p>
      <button type="button" className="quiet" disabled={pending} onClick={() => void perform(async () => {
        await load();
        onChanged();
      })}>Обновить</button>
      {connection.status === "disconnected" ?
        <button type="button" className="quiet" disabled={pending} onClick={() => void perform(async () => {
          setStatus(await request<TelegramStatus>(path + "/connect", { chat_id: connection.chat_id }));
          onChanged();
        })}>Возобновить приём</button> :
        <button type="button" className="quiet" disabled={pending} onClick={() => void disconnect()}>Отключить</button>}
    </article> : <form onSubmit={connect}>
      <p>Подключите только разрешённую тестовую группу. Имя группы не используется как ключ — нужен ID из проверенного отчёта #28.</p>
      <label>ID тестового чата<input name="chat_id" required maxLength={20} pattern="-?[0-9]+" placeholder="-1001234567890" /></label>
      <label>Название для менеджера<input name="name" maxLength={200} placeholder="ActionFlow Test — synthetic" /></label>
      <button disabled={pending}>Подключить тестовый чат</button>
    </form>}
  </section>;
}
