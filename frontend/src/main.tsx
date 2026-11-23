import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, errorMessage, request, uploadTelegram, type Manager, type Project, type ConversationMessage, type MessageRevision } from "./api/client";
import { ProjectSettingsForm } from "./features/projects/ProjectSettings";
import { AnalysisPreparation } from "./features/projects/AnalysisPreparation";
import { TelegramConnectionPanel } from "./features/projects/TelegramConnectionPanel";
import { FindingsQueue } from "./features/findings/FindingsQueue";
import "./style.css";

function Login({ onLogin }: { onLogin: (manager: Manager) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const credentials = Object.fromEntries(new FormData(event.currentTarget));
    setBusy(true);
    setError("");
    try {
      onLogin(await request<Manager>("/auth/login", credentials));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return <section className="login panel">
    <h1>Вход менеджера</h1>
    <form onSubmit={submit}>
      <label>Имя пользователя<input name="username" autoComplete="username" maxLength={120} required /></label>
      <label>Пароль<input name="password" type="password" autoComplete="current-password" maxLength={256} required /></label>
      {error && <p role="alert">{error}</p>}
      <button disabled={busy}>{busy ? "Входим…" : "Войти"}</button>
    </form>
  </section>;
}

function Projects({ onExpired }: { onExpired: () => void }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [selected, setSelected] = useState<Project | null>(null);
  const [selectionRevision, setSelectionRevision] = useState(0);
  const [participantRevision, setParticipantRevision] = useState(0);
  const [queueRevision, setQueueRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [importReport, setImportReport] = useState("");
  const [messageCursor, setMessageCursor] = useState<string | null>(null);
  const [messageHistory, setMessageHistory] = useState<{messageId: string; items: MessageRevision[]} | null>(null);
  const uploadKey = useRef<string | null>(null);

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) onExpired();
      else setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function load(cursor?: string) {
    const page = await request<{ items: Project[]; next_cursor: string | null }>(
      "/projects" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""),
    );
    setProjects((previous) => cursor && previous ? [...previous, ...page.items] : page.items);
    setNext(page.next_cursor);
  }

  useEffect(() => { void perform(() => load()); }, []);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const input = Object.fromEntries(new FormData(form));
    await perform(async () => {
      const project = await request<Project>("/projects", input);
      setSelected(project);
      form.reset();
      await load();
    });
  }

  async function loadMessages(project: Project, cursor?: string) {
    const page = cursor ? "?cursor=" + encodeURIComponent(cursor) : "";
    const result = await request<{items: ConversationMessage[]; next_cursor: string | null }>(`/projects/${project.id}/messages${page}`);
    setMessages((items) => cursor ? [...items, ...result.items] : result.items);
    setMessageCursor(result.next_cursor);
  }

  async function importFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = new FormData(event.currentTarget).get("telegram") as File;
    if (!selected || !file?.size) return;
    await perform(async () => {
      const report = await uploadTelegram<{
        imported: number; updated: number; skipped: number; missing_from_export: number;
        attachments_present: number; system_events: number; uncertain_revisions: number; unresolved_replies: number;
      }>(`/projects/${selected.id}/messages/import`, file, uploadKey.current ??= crypto.randomUUID());
      uploadKey.current = null;
      setParticipantRevision((revision) => revision + 1);
      setMessageHistory(null);
      setImportReport([
        `Новые: ${report.imported}, обновлены: ${report.updated}, пропущены: ${report.skipped}.`,
        report.missing_from_export > 0 && `Нет в этом файле: ${report.missing_from_export}. Сообщения сохранены.`,
        report.attachments_present > 0 && `Записей с необработанными вложениями: ${report.attachments_present}.`,
        report.system_events > 0 && `Системных событий: ${report.system_events}.`,
        report.uncertain_revisions > 0 && `Редакций с неясным порядком: ${report.uncertain_revisions}.`,
        report.unresolved_replies > 0 && `Ответов без исходного сообщения: ${report.unresolved_replies}.`,
      ].filter(Boolean).join(" "));
      await loadMessages(selected);
    });
  }

  return <>
    <h1>Проекты</h1>
    {busy && <p role="status">Загрузка…</p>}
    {error && <p role="alert">{error} <button type="button" className="quiet" disabled={busy} onClick={() => void perform(() => load())}>Обновить список</button></p>}
    <div className="layout">
      <section className="panel">
        <h2>Список проектов</h2>
        {projects?.length === 0 && <p>Проектов пока нет. Создайте первый проект.</p>}
        {!!projects?.length && <ul>{projects.map((project) => <li key={project.id}>
          <button className="project" disabled={busy} onClick={() => void perform(async () => {
            setSelected(await request<Project>("/projects/" + project.id));
            setMessages([]); setMessageCursor(null); setImportReport(""); setMessageHistory(null); uploadKey.current = null;
            await loadMessages(project);
            setSelectionRevision((revision) => revision + 1);
          })}>
            <strong>{project.name}</strong><span>{project.client.name}</span>
          </button>
        </li>)}</ul>}
        {next && <button className="quiet" disabled={busy} onClick={() => void perform(() => load(next))}>Ещё проекты</button>}
        {selected && <article aria-live="polite">
          <h2>{selected.name}</h2>
          <dl><dt>Клиент</dt><dd>{selected.client.name}</dd><dt>Ответственный</dt><dd>{selected.manager_name}</dd></dl>
        </article>}
      </section>
      <section className="panel">
        <h2>Новый проект</h2>
        <form onSubmit={create}>
          <label>Клиент<input name="client_name" maxLength={200} required /></label>
          <label>Название проекта<input name="project_name" maxLength={200} required /></label>
          <label>Ответственный<input name="manager_name" maxLength={200} required /></label>
          <button disabled={busy}>Создать проект</button>
        </form>
      </section>
    </div>
    {selected && <ProjectSettingsForm key={selected.id + ":" + selectionRevision} project={selected} participantRevision={participantRevision} onExpired={onExpired} onSaved={(settings) => {
      setSelected((project) => project?.id === selected.id
        ? { ...project, manager_name: settings.manager_name, settings_version: settings }
        : project);
    }} />}
    {selected && <section className="panel conversation"><h2>Переписка · {selected.name}</h2>
      <button type="button" className="quiet" disabled={busy} onClick={() => void perform(() => loadMessages(selected))}>Обновить переписку</button>
      <form onSubmit={importFile}><label>Экспорт Telegram JSON<input type="file" name="telegram" accept="application/json,.json" required onChange={() => { uploadKey.current = null; }} /></label><button disabled={busy}>Импортировать</button></form>
      {importReport && <p role="status">{importReport}</p>}
      {messages.length > 0 && <p>Экспорт показывает только доступные сообщения и редакции. Отсутствие записи не подтверждает удаление.</p>}
      {messages.map((message) => <article key={message.id} id={"message-" + message.external_id}>
        <strong>{message.source_type === "service" ? "Системное событие" : message.author.name ?? message.author.id ?? "Неизвестный автор"}</strong>
        {message.source_type === "message" && <> · {message.author.side}</>} · {message.source === "telegram_bot" ? "бот" : "экспорт"} · <time>{new Date(message.sent_at).toLocaleString()}</time>
        {message.source_type === "service" && <p>{message.service_action ?? "Неизвестное действие"} · {message.actor_name ?? "Неизвестный участник события"}</p>}
        <p>{message.text || "[Нет текста]"}</p>
        {Object.keys(message.attachments).length > 0 && <p>{message.attachments.media_type === "voice_message" || "voice_message" in message.attachments ? "Голосовое сообщение" : "Вложение"}. Содержимое не обработано.</p>}
        {message.reply_to_external_id && (messages.some((candidate) => candidate.external_id === message.reply_to_external_id)
          ? <a href={"#message-" + message.reply_to_external_id}>Перейти к сообщению, на которое ответили</a>
          : <p>Ответ на сообщение {message.reply_to_external_id} вне показанного среза.</p>)}
        <p><button type="button" className="quiet" disabled={busy} onClick={() => void perform(async () => {
          const result = await request<{items: MessageRevision[]}>(`/projects/${selected.id}/messages/${message.id}/revisions`);
          setMessageHistory({messageId: message.id, items: result.items});
        })}>Редакции</button></p>
        {messageHistory?.messageId === message.id && <ol>{messageHistory.items.map((revision) => <li key={revision.id}>
          <strong>Редакция {revision.revision_no}</strong> · {revision.ordering_uncertain ? "Порядок не подтверждён" : "Наблюдалась"}
          <p>{revision.text || "[Нет текста]"}</p>
          {revision.reply_to_external_id && <p>Ответ на сообщение {revision.reply_to_external_id}.</p>}
          {Object.keys(revision.attachments).length > 0 && <p>Содержимое вложения не обработано.</p>}
          <p>Импорт: {new Date(revision.observed_at).toLocaleString()} · Изменение в источнике: {revision.edited_at ? new Date(revision.edited_at).toLocaleString() : "время неизвестно"} · Наблюдение в источнике: {revision.source_observed_at ? new Date(revision.source_observed_at).toLocaleString() : "время неизвестно"}</p>
        </li>)}</ol>}
      </article>)}
      {selected && messages.length === 0 && !busy && <p>Сообщений пока нет.</p>}
      {messageCursor && <button className="quiet" disabled={busy} onClick={() => void perform(() => loadMessages(selected, messageCursor))}>Ещё сообщения</button>}
    </section>}
    {selected && <TelegramConnectionPanel key={selected.id + "-telegram"} projectId={selected.id} onExpired={onExpired} onChanged={() => void perform(() => loadMessages(selected))} />}
    {selected && <AnalysisPreparation key={selected.id} projectId={selected.id} onExpired={onExpired} onCompleted={() => setQueueRevision((revision) => revision + 1)} />}
    {selected && <FindingsQueue key={selected.id + "-queue-" + queueRevision} projectId={selected.id} onExpired={onExpired} />}
  </>;
}

function App() {
  const [manager, setManager] = useState<Manager | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    request<Manager>("/auth/me").then(setManager).catch((cause) => {
      if (!(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause));
    }).finally(() => setLoading(false));
  }, [attempt]);

  async function logout() {
    setLoggingOut(true);
    setError("");
    try {
      await request("/auth/logout", {});
      setManager(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) setManager(null);
      else setError(errorMessage(cause));
    } finally {
      setLoggingOut(false);
    }
  }

  return <>
    <header>
      <strong>ResponseFlow</strong>
      {manager && <div><span>{manager.username}</span><button className="quiet" disabled={loggingOut} onClick={logout}>Выйти</button></div>}
    </header>
    <main>
      {loading && <p role="status">Загрузка…</p>}
      {error && <p role="alert">{error} {!manager && <button className="quiet" onClick={() => {
        setError("");
        setLoading(true);
        setAttempt(attempt + 1);
      }}>Повторить</button>}</p>}
      {!loading && (manager ? <Projects onExpired={() => setManager(null)} /> : <Login onLogin={(account) => {
        setError("");
        setManager(account);
      }} />)}
    </main>
  </>;
}

createRoot(document.getElementById("root")!).render(<App />);
