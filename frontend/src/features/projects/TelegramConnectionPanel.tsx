import { useEffect, useState, type FormEvent } from "react";
import { ApiError, errorMessage, request, type TelegramStatus } from "../../api/client";

const labels = {
  active: "Подключён",
  needs_check: "Требует проверки",
  disconnected: "Отключён",
};

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
  }

  useEffect(() => { void perform(load); }, [path]);

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
    {status && !status.bot_configured && <p>Токен бота не настроен в локальном окружении. Подключение можно сохранить, но коннектор не запустится.</p>}
    {connection ? <article>
      <dl>
        <dt>Чат</dt><dd>{connection.name} · <code>{connection.chat_id}</code></dd>
        <dt>Состояние</dt><dd>{labels[connection.status]}</dd>
        <dt>Последнее событие</dt><dd>{connection.last_event_at ? new Date(connection.last_event_at).toLocaleString() : "Ещё не было"}</dd>
      </dl>
      {connection.last_error && <p role="alert">Требуется проверка источника: {connection.last_error}. Отсутствие новых сообщений не считается доказательством отсутствия ответа.</p>}
      <p>Чат привязан вручную по устойчивому ID. Новые текстовые сообщения, ответы, доступные редакции и нетекстовые элементы появятся в переписке после запуска коннектора. Сторону нового участника можно задать в настройках проекта.</p>
      <button type="button" className="quiet" disabled={pending} onClick={() => void perform(async () => {
        await load();
        onChanged();
      })}>Обновить</button>
      <button type="button" className="quiet" disabled={pending} onClick={() => void disconnect()}>Отключить</button>
    </article> : <form onSubmit={connect}>
      <p>Подключите только разрешённую тестовую группу. Имя группы не используется как ключ — нужен ID из проверенного отчёта #28.</p>
      <label>ID тестового чата<input name="chat_id" required maxLength={20} pattern="-?[0-9]+" placeholder="-1001234567890" /></label>
      <label>Название для менеджера<input name="name" maxLength={200} placeholder="ActionFlow Test — synthetic" /></label>
      <button disabled={pending}>Подключить тестовый чат</button>
    </form>}
  </section>;
}
