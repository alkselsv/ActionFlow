import type { FindingCard, FindingMessage } from "../../api/client";

const actionLabels: Record<string, string> = {
  reply: "Ответить клиенту по существу",
  review: "Проверить ситуацию вручную",
  follow_up: "Проконтролировать обещанный результат",
  none: "Действие не требуется",
};

const limitationLabels: Record<string, string> = {
  first_reaction_without_resolution: "Есть подтверждение или обещание, но нет решения",
  no_vedita_response: "Ответа Vedita в доступном срезе нет",
  possible_attachment_answer: "Возможный ответ находится во вложении",
  unparsed_content: "Содержимое сообщения недоступно",
  unknown_side: "Сторона участника неизвестна",
  missing_text: "Текст сообщения отсутствует",
  history_incomplete: "История может быть неполной",
  system_events: "В переписке есть системные события",
};

const priorityLabels: Record<string, string> = {
  explicit_contract_or_operational_risk: "Явный договорный или операционный риск",
  first_response_sla_breached: "Нарушен SLA первой реакции",
  uncertain_or_pending_sla: "SLA ещё не истёк или расчёт ограничен данными",
  open_request_waiting_for_response: "Открытое обращение ждёт содержательного ответа",
  commitment_deadline_missed: "Пропущен срок обязательства",
  commitment_deadline_in_future: "Срок обязательства ещё не наступил",
  commitment_without_reliable_deadline: "У обязательства нет надёжного срока",
  complaint_requires_manager_review: "Претензия требует проверки менеджера",
  unknown_finding_type: "Тип находки требует проверки",
  data_limitations_require_manual_review: "Недостаточно данных для уверенного вывода",
};

const slaStateLabels: Record<string, string> = {
  met: "первая реакция своевременна",
  breached: "первая реакция просрочена",
  pending: "срок ещё не истёк",
  uncertain: "расчёт ограничен данными",
  not_calculated: "не рассчитан",
};

const priorityValueLabels: Record<string, string> = {
  low: "низкий",
  medium: "средний",
  high: "высокий",
  unknown: "не рассчитан",
};

function reasons(value: unknown) {
  const reasons = (value as { reasons?: unknown })?.reasons;
  return Array.isArray(reasons)
    ? reasons.map((item) => priorityLabels[String(item)] ?? String(item))
    : [];
}

function evidenceText(message: FindingMessage) {
  return message.quote || message.text || "[Нет текста]";
}

function Author({ message }: { message: FindingMessage }) {
  return <span>{message.author.name ?? message.author.id ?? "Неизвестный автор"}
    {message.author.side && <> · {message.author.side}</>}
    {" · "}{new Date(message.sent_at).toLocaleString()}</span>;
}

export function FindingDetails({ card }: { card: FindingCard }) {
  return <article className="finding-detail" aria-live="polite">
    <h3>Обращение без ответа</h3>
    <dl>
      <dt>Описание</dt><dd>{card.description}</dd>
      <dt>Рекомендация</dt><dd>{actionLabels[card.recommendation] ?? card.recommendation}</dd>
      <dt>Приоритет</dt><dd>{priorityValueLabels[card.priority] ?? card.priority}</dd>
      <dt>Почему приоритет</dt><dd>{reasons(card.priority_reason).join("; ") || "правило не объяснено"}</dd>
      <dt>SLA первой реакции</dt><dd>{slaStateLabels[card.sla.state] ?? card.sla.state}</dd>
      <dt>Срок первой реакции</dt><dd>{card.sla.due_at ? new Date(card.sla.due_at).toLocaleString() : "не рассчитан"}</dd>
      <dt>Первая реакция</dt><dd>{card.sla.first_response_at ? new Date(card.sla.first_response_at).toLocaleString() : "не найдена"}</dd>
      <dt>Почему SLA</dt><dd>{card.sla.reason ? `${card.sla.reason.sla_minutes ?? "—"} рабочих минут, ${card.sla.reason.timezone ?? "часовой пояс не указан"}; оценка на ${card.sla.reason.evaluated_at ? new Date(String(card.sla.reason.evaluated_at)).toLocaleString() : "момент запуска"}.` : "версия SLA не была доступна"}</dd>
      <dt>Версия условий</dt><dd>{card.sla.settings_version_id ?? "не зафиксирована"}</dd>
      <dt>Уверенность</dt><dd>{Math.round(card.confidence * 100)}% (не измеренная вероятность правильности)</dd>
    </dl>
    {card.limitations.length > 0 && <p><strong>Ограничения:</strong> {card.limitations.map((item) => limitationLabels[item] ?? item).join("; ")}.</p>}

    <h4>Основания</h4>
    <ol className="evidence-list">{card.evidence.map((message, index) => <li key={`${message.revision_id}-${index}`} className={message.role === "anchor" ? "anchor" : ""}>
      <strong>{message.role === "anchor" ? "Исходное обращение" : "Наблюдение"}</strong>
      <p>{evidenceText(message)}</p>
      <Author message={message} />
      {Object.keys(message.attachments).length > 0 && <p>Вложение. Содержимое не анализировалось.</p>}
    </li>)}</ol>

    <h4>Окружающая переписка</h4>
    <ol className="surrounding">{card.surrounding.map((message) => <li key={message.revision_id} className={message.is_anchor ? "anchor" : ""}>
      <strong>{message.author.name ?? message.author.id ?? "Системное событие"}</strong>
      {message.author.side && <> · {message.author.side}</>}
      {" · "}<time>{new Date(message.sent_at).toLocaleString()}</time>
      <p>{message.text || "[Нет текста]"}</p>
      {message.reply_to_external_id && <p>Ответ на сообщение {message.reply_to_external_id}.</p>}
    </li>)}</ol>

    <h4>История обнаружений</h4>
    <ol>{card.history.map((item) => <li key={item.run_id}>
      {new Date(item.observed_at).toLocaleString()} · момент оценки {new Date(item.as_of).toLocaleString()} · запуск {item.run_id}
      <p>{item.summary}</p>
      <p>Приоритет: {item.priority ? priorityValueLabels[item.priority] ?? item.priority : "не рассчитан"} · SLA: {item.sla.state ? slaStateLabels[item.sla.state] ?? item.sla.state : "не рассчитан"} · версия условий {item.sla.settings_version_id ?? "не зафиксирована"}.</p>
    </li>)}</ol>

    {card.detector && <p>
      Модель: {card.detector.model_id ?? "не указана"} · prompt {card.detector.prompt_version ?? "—"} · схема {card.detector.schema_version ?? "—"} · длительность {card.detector.duration_ms ?? "—"} мс · стоимость {card.detector.cost_usd === null ? "неизвестна" : card.detector.cost_usd}.
    </p>}
    <p>На этом шаге реализован только тип <code>unanswered</code>. Решения менеджера появятся в следующих задачах.</p>
  </article>;
}
