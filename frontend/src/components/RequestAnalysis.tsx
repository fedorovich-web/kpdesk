import { useState } from 'react';
import { categoryLabel } from '../categories';
import type { RequestAnalysis, RequirementCheck } from '../tenant-session';

const statuses: Record<RequirementCheck['status'], string> = {
  provided: 'Указано в заявке', matched: 'Соответствует', mismatch: 'Не соответствует',
  missing_request: 'Нужно уточнить у клиента', unknown_product: 'Нет данных в прайсе',
  needs_review: 'Требуется проверка',
};

export function RequirementMatrix({ checks, summary = 'Проверка характеристик' }: {
  checks?: RequirementCheck[]; summary?: string;
}) {
  if (!checks?.length) return <p>Для этой позиции пока нет сравнения характеристик.</p>;
  const value = (check: RequirementCheck, text: string | null) =>
    text === null ? 'Не указано' : check.field === 'category' ? categoryLabel(text) : text;
  return <details className="tenant-requirements">
    <summary>{summary}</summary>
    <p>Сравнение с данными прайса. Наличие и сроки поставки не подтверждены.</p>
    <div className="supplier-inspection-table" role="region" tabIndex={0} aria-label="Таблица соответствия требованиям">
      <table>
        <caption>Требования заявки и характеристики товара</caption>
        <thead><tr><th scope="col">Требование</th><th scope="col">В заявке</th>
          <th scope="col">В товаре</th><th scope="col">Результат</th></tr></thead>
        <tbody>{checks.map(check => <tr key={check.field}>
          <th scope="row">{check.label}</th><td>{value(check, check.requested)}</td>
          <td>{value(check, check.actual)}</td><td>{statuses[check.status]}
            {check.note && <p>{check.note}</p>}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </details>;
}

export function RequestAnalysisPanel({ analysis, onRefresh, busy, canAnalyze }: {
  analysis: RequestAnalysis | null; onRefresh: () => void; busy: boolean; canAnalyze: boolean;
}) {
  const [copyStatus, setCopyStatus] = useState('');
  const copy = async () => {
    if (!analysis?.questions_text) return;
    try {
      await navigator.clipboard.writeText(analysis.questions_text);
      setCopyStatus('Вопросы скопированы. Проверьте их перед отправкой клиенту.');
    } catch {
      setCopyStatus('Не удалось скопировать автоматически. Выделите текст вопросов и скопируйте вручную.');
    }
  };
  return <section aria-label="Полнота заявки">
    <h3>Что уточнить у клиента</h3>
    <button type="button" className="button button-white" disabled={busy || !canAnalyze}
      onClick={onRefresh}>Проверить полноту заявки</button>
    {!analysis ? <p>Проверьте товары из заявки, затем нажмите «Проверить полноту заявки». Сервис покажет, какие сведения нужно уточнить у клиента.</p> : <>
      <p>{analysis.ready_for_matching
        ? 'Обязательные сведения для подбора заполнены. Проверьте их по исходной заявке.'
        : 'Для подбора нужны уточнения.'}</p>
      {analysis.lines.filter(line => line.questions.length > 0).map(line =>
        <div key={line.line_number}>
          <h4>Позиция {line.line_number}: {line.description}</h4>
          <blockquote>{line.source_quote}</blockquote>
          <ul>{line.questions.map(question => <li key={question}>{question}</li>)}</ul>
        </div>)}
      {analysis.questions_text && <>
        <label>Вопросы для клиента
          <textarea readOnly rows={6} value={analysis.questions_text}/>
        </label>
        <button type="button" className="button button-white" onClick={() => void copy()}>
          Скопировать вопросы</button>
        {copyStatus && <p role="status">{copyStatus}</p>}
      </>}
      <p>{analysis.scope_notice}</p>
    </>}
  </section>;
}
