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
      <dt>Приоритет</dt><dd>{card.priority === "unknown" ? "ещё не рассчитан" : card.priority}</dd>
      <dt>SLA первой реакции</dt><dd>{card.sla.state === "not_calculated" ? "ещё не рассчитан" : card.sla.state}</dd>
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
    </li>)}</ol>

    {card.detector && <p>
      Модель: {card.detector.model_id ?? "не указана"} · prompt {card.detector.prompt_version ?? "—"} · схема {card.detector.schema_version ?? "—"} · длительность {card.detector.duration_ms ?? "—"} мс · стоимость {card.detector.cost_usd === null ? "неизвестна" : card.detector.cost_usd}.
    </p>}
    <p>На этом шаге реализован только тип <code>unanswered</code>. Решения менеджера появятся в следующих задачах.</p>
  </article>;
}
