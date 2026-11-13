import { useState } from 'react';
import type { FormEvent } from 'react';
import { sessionApi } from '../tenant-session';
import type {
  CandidateAdjudicationFinding, CandidateAdjudicationHistory, CandidateConsensus,
  CandidateInspectResponse, CandidateReviewTask,
} from '../tenant-session';

type Props = {
  organizationId: string; csrf: string;
  archive: File; inspection: CandidateInspectResponse; consensus: CandidateConsensus;
};

export function TenantCandidateAdjudication({
  organizationId, csrf, archive, inspection, consensus,
}: Props) {
  const [taskId, setTaskId] = useState('');
  const [finding, setFinding] = useState<CandidateAdjudicationFinding>('needs_evidence');
  const [judgments, setJudgments] = useState<Record<string, 'accepted' | 'rejected'>>({});
  const [scopedNoMatch, setScopedNoMatch] = useState(false);
  const [rationale, setRationale] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<CandidateAdjudicationHistory | null>(null);
  const [offset, setOffset] = useState(0);
  const disputes = [
    ...consensus.conflicts,
    ...consensus.incomplete.filter(item => item.reviewer_count >= 2),
  ];
  const selected: CandidateReviewTask | null =
    inspection.tasks.find(item => item.task_sha256 === taskId) ?? null;
  const allReviewed = !!selected && selected.candidate_skus.every(
    sku => judgments[sku] === 'accepted' || judgments[sku] === 'rejected',
  );
  const allRejected = allReviewed && !!selected
    && selected.candidate_skus.every(sku => judgments[sku] === 'rejected');
  const canSubmit = !!selected && !!csrf && !busy && confirmed
    && rationale.trim().length >= 32 && rationale.trim().length <= 1000
    && (finding !== 'resolve_proposals'
      || (allReviewed && (!allRejected || scopedNoMatch)));
  const selectDispute = (newId: string) => {
    setTaskId(newId);
    setFinding('needs_evidence');
    setJudgments({});
    setScopedNoMatch(false);
    setRationale('');
    setConfirmed(false);
    setError('');
    setNotice('');
  };
  const loadHistory = async (next = 0) => {
    setBusy(true); setError('');
    try {
      const result = await sessionApi.candidateAdjudicationHistory(
        organizationId, inspection.queue_sha256, next,
      );
      if (result.independent_gold || result.training_approved) {
        throw new Error('Независимая проверка не даёт разрешения на обучение.');
      }
      setHistory(result); setOffset(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'История независимых проверок недоступна.');
    } finally {
      setBusy(false);
    }
  };
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit || !selected) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const receipt = await sessionApi.attestCandidateAdjudication(
        organizationId, archive, csrf, inspection, selected, finding,
        judgments, finding === 'resolve_proposals' && allRejected,
        rationale,
      );
      if (!receipt.recorded || !receipt.reviewer_independence_verified
        || receipt.training_approved || receipt.rights_verified || receipt.independent_gold
        || receipt.task_sha256 !== selected.task_sha256
        || receipt.queue_sha256 !== inspection.queue_sha256) {
        throw new Error('Сервер не подтвердил независимый арбитраж источника.');
      }
      setConfirmed(false); setRationale(''); setHistory(null);
      setNotice('Независимое заключение сохранено в истории без возможности изменения. Обучение запрещено.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось сохранить независимое решение.');
    } finally {
      setBusy(false);
    }
  };
  return <section className="supplier-decision" aria-label="Независимая проверка спорного подбора">
    <h3>Решение по спорному подбору · для руководителя</h3>
    <p className="access-hint">
      Арбитр не должен быть ни одним из исходных рецензентов. Решение касается
      только предъявленных товаров и не подтверждает права на документы,
      отсутствие персональных данных или допуск к обучению.
    </p>
    {error && <p role="alert" className="alert error">{error}</p>}
    {notice && <p role="status" className="alert success">{notice}</p>}
    <label>Заявка со спорной оценкой
      <select value={taskId} disabled={busy}
        onChange={event => selectDispute(event.target.value)}>
        <option value="">Выберите заявку из текущей страницы</option>
        {disputes.map(dispute => <option key={dispute.task_sha256}
          value={dispute.task_sha256}
          disabled={!inspection.tasks.some(item => item.task_sha256 === dispute.task_sha256)}>
          {dispute.request_id} — {dispute.reviewer_count} рецензента
        </option>)}
      </select>
    </label>
    {disputes.length > 0 && !disputes.some(
      item => inspection.tasks.some(task => task.task_sha256 === item.task_sha256),
    ) && <p className="access-hint">Откройте страницу очереди с конфликтной заявкой.</p>}
    {selected && <form className="access-form" onSubmit={event => void submit(event)}>
      <p>{selected.request_text}</p>
      <label>Решение по спору
        <select value={finding} disabled={busy}
          onChange={event => {
            setFinding(event.target.value as CandidateAdjudicationFinding);
            setJudgments({}); setScopedNoMatch(false); setConfirmed(false);
          }}>
          <option value="needs_evidence">Нужна дополнительная проверка</option>
          <option value="reject_case">Отклонить спорный случай</option>
          <option value="resolve_proposals">Проверено: разрешить спор только по показанным товарам</option>
        </select>
      </label>
      {finding === 'resolve_proposals' && <>
        {selected.candidates.map(product => <label key={product.sku}>
          {product.sku} — {product.name} ({product.sale_unit})
          <select value={judgments[product.sku] ?? ''} disabled={busy}
            onChange={event => {
              const choice = event.target.value;
              setJudgments(previous => {
                const next = { ...previous };
                if (choice === 'accepted' || choice === 'rejected') {
                  next[product.sku] = choice;
                } else {
                  delete next[product.sku];
                }
                return next;
              });
              setScopedNoMatch(false); setConfirmed(false);
            }}>
            <option value="">Выберите итог</option>
            <option value="accepted">Соответствует</option>
            <option value="rejected">Не соответствует</option>
          </select>
        </label>)}
        {allRejected && <label className="tenant-checkbox">
          <input type="checkbox" checked={scopedNoMatch} disabled={busy}
            onChange={event => setScopedNoMatch(event.target.checked)}/>
          Я подтверждаю отсутствие совпадений только среди показанных товаров
        </label>}
      </>}
      <label>Обоснование независимого решения
        <textarea rows={3} required minLength={32} maxLength={1000}
          value={rationale} disabled={busy}
          onChange={event => { setRationale(event.target.value); setConfirmed(false); }}/>
      </label>
      <label className="tenant-checkbox">
        <input type="checkbox" checked={confirmed} disabled={busy}
          onChange={event => setConfirmed(event.target.checked)}/>
        Я лично изучил оригинал и оба независимых заключения; не участвовал
        в исходной разметке
      </label>
      <button className="button button-primary" type="submit" disabled={!canSubmit}>
        Записать независимое решение
      </button>
    </form>}
    <button type="button" className="button button-white" disabled={busy}
      onClick={() => void loadHistory()}>История независимых решений</button>
    {history && <>
      <ul className="tenant-list">
        {history.rows.map(item => <li key={item.task_sha256}>
          <span><strong>{item.request_id}: {item.finding}</strong>
            <small>{item.original_reviewer_count} рецензента · {
              item.adjudicator_user_id
            }</small>
            <small>{item.rationale}</small>
          </span>
        </li>)}
      </ul>
      {history.next_offset !== null && <button type="button"
        className="button button-white" disabled={busy}
        onClick={() => {
          if (history.next_offset !== null) void loadHistory(history.next_offset);
        }}>Следующие независимые решения</button>}
      <small>Позиции с {offset + 1}. Проверенный набор для обучения не создан.</small>
    </>}
  </section>;
}
