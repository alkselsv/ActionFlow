import { useEffect, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, errorMessage, request, type Manager, type Project } from "./api/client";
import { ProjectSettingsForm } from "./features/projects/ProjectSettings";
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
    {selected && <ProjectSettingsForm key={selected.id + ":" + selectionRevision} project={selected} onExpired={onExpired} onSaved={(settings) => {
      setSelected((project) => project?.id === selected.id
        ? { ...project, manager_name: settings.manager_name, settings_version: settings }
        : project);
    }} />}
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
