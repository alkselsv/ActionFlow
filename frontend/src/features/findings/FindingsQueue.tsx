import { useEffect, useState } from "react";
import {
  ApiError,
  errorMessage,
  request,
  type FindingCard,
  type FindingCounts,
  type FindingSummary,
} from "../../api/client";
import { FindingDetails } from "./FindingDetails";

type Filters = {
  type: string;
  status: string;
  priority: string;
  sla: string;
};

const initialFilters: Filters = {
  type: "",
  status: "",
  priority: "",
  sla: "",
};

const typeLabels: Record<string, string> = {
  unanswered: "Обращение без ответа",
  commitment: "Обязательство Vedita",
  complaint: "Претензия клиента",
};

const commitmentStatusLabels: Record<string, string> = {
  open: "открыто",
  fulfilled: "исполнено",
  cancelled: "отменено",
  uncertain: "требует проверки",
};

const slaLabels: Record<string, string> = {
  met: "соблюдён",
  breached: "нарушен",
  pending: "ожидает",
  uncertain: "неопределён",
  not_calculated: "не рассчитан",
};

const priorityValueLabels: Record<string, string> = {
  low: "низкий",
  medium: "средний",
  high: "высокий",
  unknown: "не рассчитан",
};

const priorityLabels: Record<string, string> = {
  explicit_contract_or_operational_risk: "явный риск",
  first_response_sla_breached: "нарушен SLA первой реакции",
  uncertain_or_pending_sla: "SLA ожидает или расчёт ограничен",
  open_request_waiting_for_response: "обращение ждёт ответа",
  commitment_deadline_missed: "пропущен срок обязательства",
  commitment_deadline_missed_uncertain: "срок обязательства, возможно, пропущен",
  commitment_deadline_in_future: "срок обязательства в будущем",
  commitment_deadline_uncertain: "срок обязательства требует уточнения",
  commitment_completed: "обязательство завершено",
  commitment_without_reliable_deadline: "нет надёжного срока",
  complaint_requires_manager_review: "претензия требует проверки",
  data_limitations_require_manual_review: "недостаточно данных",
};

const limitationLabels: Record<string, string> = {
  first_reaction_without_resolution: "нет решения",
  no_vedita_response: "нет ответа Vedita",
  possible_attachment_answer: "возможный ответ во вложении",
  unparsed_content: "содержимое недоступно",
  unknown_side: "сторона неизвестна",
  missing_text: "нет текста",
  history_incomplete: "история неполна",
  system_events: "есть системные события",
  ambiguous_due: "срок неоднозначен",
  ambiguous_update: "связь изменения неоднозначна",
};

function explanation(value: Record<string, unknown> | null | undefined) {
  const items = value?.reasons;
  return Array.isArray(items)
    ? items.map((item) => priorityLabels[String(item)] ?? String(item)).join("; ")
    : "";
}

function queryString(filters: Filters) {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.status) params.set("status", filters.status);
  if (filters.priority) params.set("priority", filters.priority);
  if (filters.sla) params.set("sla", filters.sla);
  const value = params.toString();
  return value ? "?" + value : "";
}

export function FindingsQueue({
  projectId,
  onExpired,
}: {
  projectId: string;
  onExpired: () => void;
}) {
  const [filters, setFilters] = useState<Filters>(initialFilters);
  const [items, setItems] = useState<FindingSummary[]>([]);
  const [counts, setCounts] = useState<FindingCounts | null>(null);
  const [card, setCard] = useState<FindingCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  function handleError(cause: unknown) {
    if (cause instanceof ApiError && cause.status === 401) onExpired();
    else setError(errorMessage(cause));
  }

  useEffect(() => {
    let active = true;
    setBusy(true);
    setError("");
    Promise.all([
      request<{ items: FindingSummary[] }>(
        `/projects/${projectId}/findings${queryString(filters)}`,
      ),
      request<FindingCounts>(`/projects/${projectId}/findings/counts`),
    ])
      .then(([findings, nextCounts]) => {
        if (!active) return;
        setItems(findings.items);
        setCounts(nextCounts);
        setCard((current) =>
          current && findings.items.some((item) => item.id === current.id)
            ? current
            : null,
        );
      })
      .catch((cause) => {
        if (active) handleError(cause);
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [projectId, filters, refresh]);

  async function openCard(id: string) {
    setError("");
    try {
      setCard(await request<FindingCard>(`/findings/${id}`));
    } catch (cause) {
      handleError(cause);
    }
  }

  function setFilter(key: keyof Filters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  return <section className="panel queue">
    <h2>Очередь ситуаций</h2>
    <p>Сохранённые карточки проекта. Расчёт приоритета и SLA относится к версии условий выбранного запуска.</p>
    <div className="filters">
      <label>Тип<select value={filters.type} onChange={(event) => setFilter("type", event.target.value)}>
        <option value="">Все доступные</option>
        <option value="unanswered">Обращения без ответа</option>
        <option value="commitment">Обязательства Vedita</option>
        <option value="complaint">Претензии клиента</option>
      </select></label>
      <label>Статус<select value={filters.status} onChange={(event) => setFilter("status", event.target.value)}>
        <option value="">Все</option><option value="new">Новые</option><option value="confirmed">Подтверждённые</option>
        <option value="rejected">Отклонённые</option><option value="snoozed">Отложенные</option><option value="closed">Закрытые</option>
      </select></label>
      <label>Приоритет<select value={filters.priority} onChange={(event) => setFilter("priority", event.target.value)}>
        <option value="">Все</option><option value="high">Высокий</option><option value="medium">Средний</option>
        <option value="low">Низкий</option><option value="unknown">Не рассчитан</option>
      </select></label>
      <label>SLA<select value={filters.sla} onChange={(event) => setFilter("sla", event.target.value)}>
        <option value="">Все</option><option value="breached">Нарушен</option><option value="pending">Ожидает</option>
        <option value="met">Соблюдён</option><option value="uncertain">Неопределён</option>
        <option value="not_calculated">Не рассчитан</option>
      </select></label>
    </div>
    {counts && <p className="queue-counts">Открытых: {counts.open} · нарушений SLA: {counts.sla_breaches} · всего: {counts.total}.</p>}
    {busy && <p role="status">Загрузка очереди…</p>}
    {error && <p role="alert">{error}</p>}
    {!busy && items.length === 0 && <p>По выбранным фильтрам карточек нет.</p>}
    {items.length > 0 && <div className="findings-layout queue-layout">
      <ul className="finding-list">{items.map((item) => <li key={item.id}>
        <button className={"project" + (card?.id === item.id ? " selected" : "")} onClick={() => void openCard(item.id)}>
          <strong>{typeLabels[item.type] ?? item.type}</strong>
          <span>{item.description}</span>
          <span>Приоритет: {priorityValueLabels[item.priority] ?? item.priority} · {item.type === "commitment" ? `обязательство: ${item.commitment?.status ? commitmentStatusLabels[item.commitment.status] ?? item.commitment.status : "неизвестно"}` : item.type === "complaint" ? "претензия клиента" : `SLA: ${slaLabels[item.sla.state] ?? item.sla.state}`} · версия {item.version}</span>
          {item.commitment?.due_at && <span>Срок обязательства: {new Date(item.commitment.due_at).toLocaleString()}.</span>}
          {explanation(item.priority_reason) && <span>Почему: {explanation(item.priority_reason)}.</span>}
          {item.sla.due_at && <span>Срок: {new Date(item.sla.due_at).toLocaleString()} · оценка: {item.sla.reason?.evaluated_at ? new Date(String(item.sla.reason.evaluated_at)).toLocaleString() : "не указана"}.</span>}
          {item.as_of && <span>Момент анализа: {new Date(item.as_of).toLocaleString()}.</span>}
          {item.limitations.length > 0 && <span>Ограничения: {item.limitations.map((limitation) => limitationLabels[limitation] ?? limitation).join("; ")}.</span>}
        </button>
      </li>)}</ul>
      {card && <FindingDetails card={card} onUpdated={(updated) => { setCard(updated); setRefresh((value) => value + 1); }} onExpired={onExpired} />}
    </div>}
  </section>;
}
