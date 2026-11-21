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
