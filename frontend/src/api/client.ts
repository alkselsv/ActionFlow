const base = "http://127.0.0.1:8000/api/v1";

export type Manager = { username: string };
export type Project = {
  id: string;
  client: { id: string; name: string };
  name: string;
  manager_name: string;
  message_count: number;
  settings_version: ProjectSettings | null;
};
export type MessageRevision = {
  id: string;
  revision_no: number;
  text: string;
  reply_to_external_id: string | null;
  source_type: "message" | "service";
  service_action: string | null;
  actor_name: string | null;
  attachments: Record<string, unknown>;
  edited_at: string | null;
  observed_at: string;
  source_observed_at: string | null;
  ordering_uncertain: boolean;
};

export type ConversationMessage = MessageRevision & {
  revision_id: string;
  external_id: string;
  sent_at: string;
  author: { id: string | null; name: string | null; side: Side | null };
};

export type Side = "vedita" | "customer" | "unknown";
export type Participant = {
  source_author_id: string;
  display_name: string | null;
  imported: boolean;
  side: Side;
};
export type CommitmentEvent = {
  kind: string;
  at: string;
  due_at: string | null;
  previous_due_at: string | null;
  uncertain: boolean;
  message_id: string | null;
  revision_id: string | null;
};

export type CommitmentInfo = {
  id?: string;
  subject_text?: string;
  assignee_name?: string | null;
  due_at: string | null;
  status: string | null;
  events?: CommitmentEvent[];
};

export type FindingSummary = {
  id: string;
  project_id: string;
  type: "unanswered" | "commitment";
  status: "new" | "confirmed" | "rejected" | "snoozed" | "closed";
  priority: "low" | "medium" | "high" | "unknown";
  priority_reason: Record<string, unknown>;
  description: string;
  confidence: number;
  sla: {
    due_at: string | null;
    state: string;
    first_response_at: string | null;
    reason: Record<string, unknown> | null;
    settings_version_id: string | null;
  };
  limitations: string[];
  commitment: CommitmentInfo | null;
  anchor_message_id: string;
  current_run_id: string | null;
  run_id: string | null;
  as_of: string | null;
  version: number;
  updated_at: string;
};
export type FindingMessage = {
  message_id: string;
  revision_id: string;
  external_id: string;
  role: string | null;
  quote: string | null;
  text: string;
  sent_at: string;
  author: { id: string | null; name: string | null; side: Side | null };
  source_type: "message" | "service";
  attachments: Record<string, unknown>;
  reply_to_external_id: string | null;
};
export type FindingCard = FindingSummary & {
  recommendation: string;
  priority_reason: Record<string, unknown>;
  subject: {
    kind: string;
    id: string;
    anchor_message_id: string;
    anchor_revision_id: string;
  };
  limitations: string[];
  candidate: {
    candidate_id: string;
    candidate_key: string;
    finding_type: string;
    status: "valid" | "invalid";
    validation_errors: string[];
    description: string;
    confidence: number;
    priority_hint: string;
    suggested_action: string;
    limitations: string[];
    commitment?: {
      event: string;
      subject_text: string | null;
      due_expression: string | null;
      assignee_name: string | null;
      explicit_risk: boolean;
    } | null;
    subject: {
      kind: string;
      anchor_message_id: string;
      anchor_revision_id: string;
    };
    evidence: FindingMessage[];
  } | null;
  evidence: FindingMessage[];
  surrounding: (FindingMessage & { is_anchor: boolean })[];
  history: {
    run_id: string;
    candidate_id: string;
    observed_at: string;
    as_of: string;
    summary: string;
    priority: "low" | "medium" | "high" | "unknown" | null;
    priority_reason: Record<string, unknown> | null;
    commitment: CommitmentInfo | null;
    sla: {
      due_at: string | null;
      state: string | null;
      first_response_at: string | null;
      reason: Record<string, unknown> | null;
      settings_version_id: string | null;
    };
  }[];
  detector: {
    provider: string | null;
    model_id: string | null;
    detector_version: string | null;
    prompt_version: string | null;
    schema_version: string | null;
    duration_ms: number | null;
    usage: Record<string, unknown> | null;
    cost_usd: number | null;
  } | null;
  first_response_at: string | null;
};

export type FindingCounts = {
  total: number;
  open: number;
  sla_breaches: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
  by_type: Record<string, number>;
  by_sla: Record<string, number>;
};

export type ProjectSettings = {
  id: string;
  version_no: number;
  created_at: string;
  context_text: string;
  agreements: string;
  manager_name: string;
  client_type: "north" | "south" | "custom";
  work_hours: { weekdays: number[]; start: string; end: string };
  timezone: string;
  first_response_minutes: number;
  calendar: { closed_dates: string[] };
  participant_sides: Pick<Participant, "source_author_id" | "side">[];
};

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export async function request<T>(path: string, body?: object, headers: Record<string, string> = {}): Promise<T> {
  const response = await fetch(base + path, {
    credentials: "include",
    ...(body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(response.status, payload?.error?.message ?? "Не удалось выполнить запрос");
  }
  return response.status === 204 ? undefined as T : response.json();
}

export async function uploadTelegram<T>(path: string, file: File, key: string): Promise<T> {
  const response = await fetch(base + path, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: file });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(response.status, payload?.error?.message ?? "Не удалось загрузить экспорт");
  }
  return response.json();
}

export function errorMessage(error: unknown) {
  return error instanceof ApiError ? error.message : "Сервер недоступен. Проверьте запуск приложения.";
}
