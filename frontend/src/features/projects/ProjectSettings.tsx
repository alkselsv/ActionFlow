import { useEffect, useState, type FormEvent } from "react";
import { ApiError, errorMessage, request, type Participant, type Project, type ProjectSettings, type Side } from "../../api/client";

const weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const presets = { north: 60, south: 120 };
type Page = { items: ProjectSettings[]; next_cursor: string | null };

export function ProjectSettingsForm({ project, participantRevision, onSaved, onExpired }: {
  project: Project;
  participantRevision: number;
  onSaved: (settings: ProjectSettings) => void;
  onExpired: () => void;
}) {
  const [current, setCurrent] = useState(project.settings_version);
  const path = "/projects/" + project.id;
  const [participants, setParticipants] = useState<Participant[]>([]);
  const [history, setHistory] = useState<Page | null>(null);
  const [version, setVersion] = useState<ProjectSettings | null>(null);
  const [clientType, setClientType] = useState<ProjectSettings["client_type"]>(current?.client_type ?? "custom");
  const [minutes, setMinutes] = useState(String(current?.first_response_minutes ?? ""));
  const [pending, setPending] = useState(true);
  const [loaded, setLoaded] = useState(false);
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
    const [people, versions] = await Promise.all([
      request<{ items: Participant[]; settings_version_id: string | null }>(path + "/participants"),
      request<Page>(path + "/settings/versions"),
    ]);
    const latest = versions.items[0] ?? null;
    if (people.settings_version_id !== (latest?.id ?? null)) {
      throw new ApiError(409, "Настройки изменились во время загрузки. Обновите форму.");
    }
    setCurrent(latest);
    setClientType(latest?.client_type ?? "custom");
    setMinutes(String(latest?.first_response_minutes ?? ""));
    setParticipants(people.items);
    setHistory(versions);
    setLoaded(true);
  }

  useEffect(() => { void perform(load); }, []);

  useEffect(() => {
    if (!participantRevision || !loaded) return;
    let active = true;
    request<{ items: Participant[] }>(path + "/participants").then(({ items }) => {
      if (!active) return;
      setParticipants((draft) => {
        const imported = new Map(items.map((item) => [item.source_author_id, item]));
        const updated = draft.map((item) => {
          const found = imported.get(item.source_author_id);
          imported.delete(item.source_author_id);
          return found ? { ...found, side: item.side } : item;
        });
        return [...updated, ...imported.values()];
      });
    }).catch((cause) => {
      if (!active) return;
      if (cause instanceof ApiError && cause.status === 401) onExpired();
      else setError(errorMessage(cause));
    });
    return () => { active = false; };
  }, [participantRevision, loaded, path]);

  function addParticipant() {
    setParticipants([...participants, { source_author_id: "", display_name: null, imported: false, side: "unknown" }]);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setNotice("");
    await perform(async () => {
      const result = await request<ProjectSettings>(path + "/settings", {
        base_version_id: current?.id ?? null,
        context_text: data.get("context_text"),
        agreements: data.get("agreements"),
        manager_name: data.get("manager_name"),
        client_type: clientType,
        first_response_minutes: Number(minutes),
        timezone: data.get("timezone"),
        work_hours: {
          weekdays: data.getAll("weekday").map(Number),
          start: data.get("start"),
          end: data.get("end"),
        },
        calendar: { closed_dates: String(data.get("closed_dates")).split(/[\s,]+/).filter(Boolean) },
        participant_sides: participants
          .filter(({ imported, side }) => !imported || side !== "unknown")
          .map(({ source_author_id, side }) => ({ source_author_id, side })),
      });
      setCurrent(result);
      onSaved(result);
      setNotice("Версия " + result.version_no + " сохранена");
      await load();
    });
  }

  return <section className="panel settings">
    <h2>Настройки проекта</h2>
    {pending && <p role="status">Загрузка настроек…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert">{error} <button type="button" className="quiet" disabled={pending} onClick={() => void perform(load)}>Обновить настройки</button></p>}
    {loaded && <>
      <form key={current?.id ?? "new"} onSubmit={save}>
        <fieldset disabled={pending}>
          <legend>Контекст</legend>
          <label>Описание проекта<textarea name="context_text" maxLength={20000} defaultValue={current?.context_text} /></label>
          <label>Важные договорённости<textarea name="agreements" maxLength={20000} defaultValue={current?.agreements} /></label>
          <label>Ответственный<input name="manager_name" defaultValue={current?.manager_name ?? project.manager_name} required maxLength={200} /></label>
        </fieldset>
        <fieldset disabled={pending}>
          <legend>SLA первой реакции</legend>
          <label>Профиль клиента<select value={clientType} onChange={(event) => {
            const type = event.target.value as ProjectSettings["client_type"];
            setClientType(type);
            if (type !== "custom") setMinutes(String(presets[type]));
          }}>
            <option value="custom">Ручные настройки</option>
            <option value="north">Север · 60 минут</option>
            <option value="south">Юг · 120 минут</option>
          </select></label>
          <label>Рабочих минут до первой реакции<input type="number" min={1} max={525600} required value={minutes} onChange={(event) => setMinutes(event.target.value)} /></label>
          <label>Часовой пояс IANA<input name="timezone" maxLength={100} required defaultValue={current?.timezone ?? "Europe/Moscow"} /></label>
          <div className="weekdays" aria-label="Рабочие дни">{weekdays.map((day, index) => <label key={day}>
            <input type="checkbox" name="weekday" value={index + 1} defaultChecked={(current?.work_hours.weekdays ?? [1, 2, 3, 4, 5]).includes(index + 1)} />
            {day}
          </label>)}</div>
          <div className="time-window">
            <label>Начало дня<input name="start" type="time" required defaultValue={current?.work_hours.start ?? "09:00"} /></label>
            <label>Конец дня<input name="end" type="time" required defaultValue={current?.work_hours.end ?? "18:00"} /></label>
          </div>
          <label>Нерабочие даты<textarea name="closed_dates" placeholder="ГГГГ-ММ-ДД, по одной дате на строку" defaultValue={current?.calendar.closed_dates.join("\n")} /></label>
        </fieldset>
        <fieldset disabled={pending}>
          <legend>Стороны участников</legend>
          <p>До импорта можно задать сторону по ID автора, например <code>user9001</code>.</p>
          {participants.map((participant, index) => <div className="participant" key={index}>
            {participant.display_name && <strong>{participant.display_name}</strong>}
            <label>ID участника<input required maxLength={200} value={participant.source_author_id} readOnly={participant.imported} onChange={(event) => {
              setParticipants(participants.map((row, rowIndex) => rowIndex === index ? { ...row, source_author_id: event.target.value } : row));
            }} /></label>
            <label>Сторона<select value={participant.side} onChange={(event) => {
              setParticipants(participants.map((row, rowIndex) => rowIndex === index ? { ...row, side: event.target.value as Side } : row));
            }}>
              <option value="unknown">Неизвестно</option>
              <option value="vedita">Vedita</option>
              <option value="customer">Клиент</option>
            </select></label>
            {!participant.imported && <button className="quiet" type="button" onClick={() => setParticipants(participants.filter((_, rowIndex) => rowIndex !== index))}>Удалить</button>}
          </div>)}
          <button className="quiet" type="button" onClick={addParticipant}>Добавить участника</button>
        </fieldset>
        <button disabled={pending}>Сохранить новую версию</button>
      </form>
      <h3>История настроек</h3>
      {history?.items.length === 0 && <p>Сохранённых версий пока нет.</p>}
      {history?.items.map((item) => <button className="quiet" type="button" key={item.id} disabled={pending} onClick={() => setVersion(item)}>
        Версия {item.version_no}
      </button>)}
      {history?.next_cursor && <button className="quiet" disabled={pending} onClick={() => void perform(async () => {
        const page = await request<Page>(path + "/settings/versions?cursor=" + encodeURIComponent(history.next_cursor!));
        setHistory({ items: [...history.items, ...page.items], next_cursor: page.next_cursor });
      })}>Ещё версии</button>}
      {version && <article>
        <h3>Версия {version.version_no}</h3>
        <dl>
          <dt>Ответственный</dt><dd>{version.manager_name}</dd>
          <dt>Описание</dt><dd>{version.context_text || "—"}</dd>
          <dt>Договорённости</dt><dd>{version.agreements || "—"}</dd>
          <dt>Первая реакция</dt><dd>{version.first_response_minutes} мин.</dd>
          <dt>Рабочий календарь</dt><dd>{version.work_hours.weekdays.map((day) => weekdays[day - 1]).join(", ")} · {version.work_hours.start}–{version.work_hours.end} · {version.timezone}</dd>
          <dt>Нерабочие даты</dt><dd>{version.calendar.closed_dates.join(", ") || "—"}</dd>
          <dt>Участники</dt><dd>{version.participant_sides.map((row) => row.source_author_id + " — " + ({ vedita: "Vedita", customer: "Клиент", unknown: "Неизвестно" })[row.side]).join(", ") || "—"}</dd>
        </dl>
      </article>}
    </>}
  </section>;
}
