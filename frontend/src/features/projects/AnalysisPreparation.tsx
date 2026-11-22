import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, errorMessage, request, type FindingCard, type FindingSummary } from "../../api/client";
import { FindingDetails } from "../findings/FindingDetails";

type Run = {
  run_id: string;
  status: "pending" | "queued" | "running" | "succeeded" | "failed";
  stage: string;
  error_code: string | null;
  retryable: boolean | null;
  as_of: string;
  apply_to_queue: boolean;
  settings_version_id: string | null;
  input_hash: string | null;
  limitations: {
    message_count: number; attachments: number; system_events: number;
    unknown_authors: number; missing_settings: boolean; unavailable_messages: number;
    context_messages: number; uncertain_revisions: number;
    unanswered?: { candidate_count: number; valid_candidate_count: number; invalid_candidate_count: number; limitations: string[] };
  } | null;
  candidate_count: number | null;
  invalid_candidate_count: number | null;
  finding_count: number | null;
  implemented_types: string[];
  cost_usd: number | null;
  usage: Record<string, unknown> | null;
  detector: {
    provider: string | null; model_id: string | null; detector_version: string | null;
    prompt_version: string | null; schema_version: string | null;
    parameters: Record<string, unknown> | null; duration_ms: number | null;
  } | null;
};
type InputPage = {
  items: { message_id: string; revision_id: string; role: string; participant_side: string; revision: { text: string; revision_no: number } }[];
  next_cursor: string | null;
};

const statusLabels = {
  pending: "Ожидает запуска", queued: "В очереди", running: "Анализ выполняется",
  succeeded: "Анализ завершён", failed: "Ошибка анализа",
};
const stageLabels: Record<string, string> = {
  dispatch: "доставка команды", prepare: "подготовка входа", detector: "поиск обращений",
  unanswered_ready: "обращения без ответа", commitment_ready: "обязательства",
  complaints_ready: "претензии", findings_ready: "находки готовы", finalize: "завершение",
};
const findingTypeLabels: Record<string, string> = {
  unanswered: "Обращение без ответа",
  commitment: "Обязательство Vedita",
  complaint: "Претензия клиента",
};
const errors: Record<string, string> = {
  analysis_input_too_large: "В срезе больше 50 000 сообщений. Сократите период.",
  analysis_input_timeout: "Истекло время ожидания подготовки.",
  analysis_input_failed: "Не удалось подготовить вход.",
  workflow_start_failed: "Temporal не принял запуск. Проверьте Temporal и диспетчер.",
  provider_rate_limited: "Провайдер ограничил частоту запросов; запуск можно повторить.",
  provider_timeout: "Провайдер не ответил вовремя; запуск можно повторить.",
  provider_error: "Провайдер вернул постоянную ошибку.",
  detector_invalid_output: "Модель вернула ответ, не прошедший проверку схемы.",
  unanswered_detection_failed: "Не удалось выполнить поиск обращений.",
  analysis_finalize_failed: "Не удалось сохранить завершение анализа.",
  analysis_workflow_failed: "Workflow завершился с ошибкой.",
};

export function AnalysisPreparation({ projectId, onExpired, onCompleted }: {
  projectId: string; onExpired: () => void; onCompleted?: () => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [history, setHistory] = useState<Run[]>([]);
  const [page, setPage] = useState<InputPage | null>(null);
  const [findings, setFindings] = useState<FindingSummary[]>([]);
  const [card, setCard] = useState<FindingCard | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);
  const retry = useRef<{ key: string; body: string } | null>(null);
  const projectPath = "/projects/" + projectId + "/analyses";
  const runId = run?.run_id;
  const status = run?.status;

  function handleError(cause: unknown) {
    if (cause instanceof ApiError && cause.status === 401) onExpired();
    else setError(errorMessage(cause));
  }

  useEffect(() => {
    let active = true;
    request<{items: Run[]}>(projectPath).then(({ items }) => {
      if (active) setHistory(items);
    }).catch((cause) => { if (active) handleError(cause); });
    return () => { active = false; };
  }, [projectPath]);

  useEffect(() => {
    if (!runId || status === "succeeded" || status === "failed") return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await request<Run>("/analyses/" + runId);
        if (!active) return;
        setRun(next);
        if (next.status !== "succeeded" && next.status !== "failed") timer = setTimeout(poll, 1200);
      } catch (cause) {
        if (active) handleError(cause);
      }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [runId, status, pollAttempt]);

  useEffect(() => {
    if (!runId || status !== "succeeded") {
      setFindings([]);
      setCard(null);
      return;
    }
    let active = true;
    request<{items: FindingSummary[]}>(`/analyses/${runId}/findings`).then(async ({ items }) => {
      if (!active) return;
      setFindings(items);
      if (items.length > 0) {
        const detail = await request<FindingCard>(`/findings/${items[0].id}?run_id=${encodeURIComponent(runId)}`);
        if (active) setCard(detail);
      } else {
        setCard(null);
      }
      if (active) onCompleted?.();
    }).catch((cause) => { if (active) handleError(cause); });
    return () => { active = false; };
  }, [runId, status]);

  function selectRun(item: Run) {
    setPage(null);
    setError("");
    setFindings([]);
    setCard(null);
    setRun(item);
    setPollAttempt((attempt) => attempt + 1);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const form = new FormData(event.currentTarget);
      const date = (key: string) => new Date(String(form.get(key))).toISOString();
      const payload = { period_start: date("period_start"), period_end: date("period_end"), as_of: date("as_of"), apply_to_queue: true };
      const body = JSON.stringify(payload);
      if (!retry.current || retry.current.body !== body) retry.current = { key: crypto.randomUUID(), body };
      const created = await request<{ run_id: string }>(projectPath, payload, { "Idempotency-Key": retry.current.key });
      const detail = await request<Run>("/analyses/" + created.run_id);
      retry.current = null;
      selectRun(detail);
      setHistory((items) => [detail, ...items.filter((item) => item.run_id !== detail.run_id)]);
    } catch (cause) {
      handleError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function showInput(cursor?: string) {
    if (!runId) return;
    setBusy(true);
    setError("");
    try {
      const result = await request<InputPage>("/analyses/" + runId + "/input" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
      setPage((current) => ({ ...result, items: cursor && current ? [...current.items, ...result.items] : result.items }));
    } catch (cause) {
      handleError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function selectFinding(id: string) {
    if (!runId) return;
    setError("");
    try {
      setCard(await request<FindingCard>(`/findings/${id}?run_id=${encodeURIComponent(runId)}`));
    } catch (cause) {
      handleError(cause);
    }
  }

  const stage = run ? stageLabels[run.stage] : null;
  return <section className="panel settings">
    <h2>Запуск анализа</h2>
    <p>Кнопка запускает локальные детекторы обращений без ответа, обязательств Vedita и претензий клиента на зафиксированном входе. Внешние LLM и платные вызовы не выполняются.</p>
    <form onSubmit={submit}>
      <label>Начало периода<input name="period_start" type="datetime-local" required /></label>
      <label>Конец периода, не включительно<input name="period_end" type="datetime-local" required /></label>
      <label>Момент оценки<input name="as_of" type="datetime-local" required /></label>
      <button disabled={busy}>Запустить анализ</button>
    </form>
    {error && <p role="alert">{error} {run && status !== "succeeded" && status !== "failed" &&
      <button className="quiet" onClick={() => { setError(""); setPollAttempt((attempt) => attempt + 1); }}>Обновить статус</button>}</p>}
    {history.length > 0 && <label>Сохранённые запуски<select disabled={busy} value={runId ?? ""}
      onChange={(event) => {
        const item = history.find((candidate) => candidate.run_id === event.target.value);
        if (item) selectRun(item);
      }}>
      <option value="">Выбрать запуск</option>
      {history.map((item) => <option key={item.run_id} value={item.run_id}>{new Date(item.as_of).toLocaleString()} · {item.run_id}</option>)}
    </select></label>}
    {run && <article>
      <p role="status">{statusLabels[run.status]}{stage ? ` · ${stage}` : ""} · {run.run_id}</p>
      {run.error_code && <p role="alert">{errors[run.error_code] ?? "Не удалось выполнить анализ."}
        {run.retryable && " После проверки окружения можно создать новый запуск."}</p>}
      {run.limitations && <>
        <p>Сообщений: {run.limitations.message_count}, из них контекст: {run.limitations.context_messages}. Системных событий: {run.limitations.system_events}. С необработанными вложениями: {run.limitations.attachments}. С неизвестной стороной: {run.limitations.unknown_authors}.</p>
        <p>Без доступной редакции на момент оценки: {run.limitations.unavailable_messages}. Экспорт содержит только наблюдённые версии.</p>
        {run.limitations.uncertain_revisions > 0 && <p>Не включено редакций с неопределённым порядком: {run.limitations.uncertain_revisions}.</p>}
        <p>{run.settings_version_id ? "Версия контекста и SLA: " + run.settings_version_id : "На момент оценки не было доступной версии контекста и SLA."}</p>
        {!page && <button className="quiet" disabled={busy} onClick={() => void showInput()}>Открыть сохранённый вход</button>}
      </>}
      {run.status === "succeeded" && <>
        <h3>Результат запуска</h3>
        <p>Доступны типы: {run.implemented_types.map((item) => findingTypeLabels[item] ?? item).join(", ")}. Кандидатов: {run.candidate_count ?? 0}; карточек: {run.finding_count ?? 0}; отклонено проверкой оснований: {run.invalid_candidate_count ?? 0}.</p>
        {!run.apply_to_queue && <p>Это сравнительный запуск: кандидаты сохранены, но карточки очереди не обновлялись.</p>}
        {run.detector && <p>Модель: {run.detector.model_id ?? "не указана"} · prompt {run.detector.prompt_version ?? "—"} · схема {run.detector.schema_version ?? "—"} · длительность {run.detector.duration_ms ?? "—"} мс · стоимость {run.cost_usd === null ? "неизвестна" : run.cost_usd}.</p>}
        {findings.length === 0 && <p>Находок выбранных типов не найдено.</p>}
        {findings.length > 0 && <div className="findings-layout">
          <ul className="finding-list">{findings.map((finding) => <li key={finding.id}>
            <button className={"project" + (card?.id === finding.id ? " selected" : "")} onClick={() => void selectFinding(finding.id)}>
              <strong>{findingTypeLabels[finding.type] ?? finding.type}</strong>
              <span>{finding.description}</span>
              <span>Приоритет: {finding.priority === "unknown" ? "не рассчитан" : finding.priority} · версия {finding.version}</span>
            </button>
          </li>)}</ul>
          {card && <FindingDetails card={card} />}
        </div>}
      </>}
      {page && <>
        <h4>Сохранённый вход</h4>
        {page.items.length === 0 && <p>В сохранённом срезе нет сообщений.</p>}
        <ol>{page.items.map((item) => <li key={item.revision_id}>
          {item.role === "context" ? "Контекст" : "Окно"} · {item.participant_side} · редакция {item.revision.revision_no} · {item.revision.text || "[Нет текста]"}
        </li>)}</ol>
        {page.next_cursor && <button className="quiet" disabled={busy} onClick={() => void showInput(page.next_cursor!)}>Ещё сообщения</button>}
      </>}
    </article>}
  </section>;
}
