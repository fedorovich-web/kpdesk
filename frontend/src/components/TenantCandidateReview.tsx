import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { sessionApi } from '../tenant-session';
import { TenantCandidateAdjudication } from './TenantCandidateAdjudication';
import type {
  CandidateConsensus, CandidateHistory, CandidateInspectResponse, CandidateJudgment,
  CandidateReviewTask, SourceRestrictionFinding, SourceRestrictionHistory,
} from '../tenant-session';

type Props = { organizationId: string; csrf: string | null; canAdjudicate?: boolean };

export function TenantCandidateReview({ organizationId, csrf, canAdjudicate = false }: Props) {
  const [archive, setArchive] = useState<File | null>(null);
  const [inspection, setInspection] = useState<CandidateInspectResponse | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [judgments, setJudgments] = useState<Record<string, CandidateJudgment>>({});
  const [rationale, setRationale] = useState('');
  const [noMatchChecked, setNoMatchChecked] = useState(false);
  const [personal, setPersonal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<CandidateHistory | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [consensus, setConsensus] = useState<CandidateConsensus | null>(null);
  const [sourceHistory, setSourceHistory] = useState<SourceRestrictionHistory | null>(null);
  const [sourceSha, setSourceSha] = useState('');
  const [sourceKind, setSourceKind] = useState<'rights' | 'privacy'>('rights');
  const [sourceFinding, setSourceFinding] = useState<SourceRestrictionFinding>('requires_evidence');
  const [sourceReason, setSourceReason] = useState('');
  const [sourceConfirmed, setSourceConfirmed] = useState(false);
  const generation = useRef(0);
  const selected = inspection?.tasks.find(task => task.request_id === selectedId) ?? null;

  const resetDecision = () => {
    setSelectedId('');
    setJudgments({});
    setRationale('');
    setPersonal(false);
    setNoMatchChecked(false);
  };
  const chooseArchive = (file: File | null) => {
    generation.current += 1;
    setArchive(file);
    setInspection(null);
    setHistory(null);
    setConsensus(null);
    setSourceHistory(null);
    setSourceSha('');
    setSourceConfirmed(false);
    setError('');
    setNotice('');
    resetDecision();
  };
  const inspect = async (offset = 0) => {
    if (!csrf || !archive || !archive.name.toLowerCase().endsWith('.zip')
      || archive.size > 8_000_000 || archive.size < 1) {
      setError('Нужен ZIP-архив оригиналов до 8 МБ и действующая сессия.');
      return;
    }
    const current = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const result = await sessionApi.inspectCandidateReview(organizationId, archive, csrf, offset);
      if (current !== generation.current) return;
      if (result.independent_gold || result.training_approved
        || result.rights_verified || result.privacy_review_complete
        || result.offset !== offset) {
        throw new Error('Не удалось открыть список позиций для ручной проверки.');
      }
      setInspection(result);
      setHistory(null);
      setConsensus(null);
      setSourceHistory(null);
      setSourceSha('');
      setSourceConfirmed(false);
      resetDecision();
    } catch (cause) {
      if (current === generation.current) {
        setInspection(null);
        setError(cause instanceof Error ? cause.message : 'Не удалось проверить ZIP-оригиналы.');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const chooseTask = (task: CandidateReviewTask) => {
    resetDecision();
    setSelectedId(task.request_id);
    setNotice('');
  };
  const allReviewed = !!selected && selected.candidate_skus.every(sku => !!judgments[sku]);
  const allRejected = allReviewed && !!selected
    && selected.candidate_skus.every(sku => judgments[sku] === 'rejected');
  const canSubmit = !!csrf && !!archive && !!inspection && allReviewed
    && rationale.trim().length >= 32 && rationale.trim().length <= 1000
    && personal && (!allRejected || noMatchChecked) && !busy;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit || !archive || !csrf || !inspection || !selected) return;
    const current = generation.current;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const saved = await sessionApi.attestCandidateReview(
        organizationId, archive, csrf, inspection, selected,
        judgments, rationale, allRejected,
      );
      if (current !== generation.current) return;
      if (!saved.recorded || saved.training_approved || saved.independent_gold
        || saved.rights_verified || saved.queue_sha256 !== inspection.queue_sha256
        || saved.task_sha256 !== selected.task_sha256
        || saved.request_id !== selected.request_id) {
        throw new Error('Не удалось подтвердить сохранение оценки для этой заявки. Проверьте историю перед повторной попыткой.');
      }
      resetDecision();
      setHistory(null);
      setConsensus(null);
      setNotice('Оценка сохранена в истории проверок. Это не разрешение на обучение модели или публикацию данных.');
    } catch (cause) {
      if (current === generation.current) {
        setError(cause instanceof Error ? cause.message : 'Не удалось записать решение.');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const loadHistory = async (offset = 0) => {
    if (!inspection) return;
    const current = generation.current;
    setBusy(true);
    setError('');
    try {
      const result = await sessionApi.candidateReviewHistory(
        organizationId, inspection.queue_sha256, offset,
      );
      if (current !== generation.current) return;
      if (result.independent_gold || result.training_approved) {
        throw new Error('История проверок не даёт разрешения на обучение модели.');
      }
      setHistory(result);
      setHistoryOffset(offset);
    } catch (cause) {
      if (current === generation.current) {
        setError(cause instanceof Error ? cause.message : 'История аудита недоступна.');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const sourceOptions = inspection
    ? [...new Set(inspection.tasks.map(task => task.source_sha256))] : [];
  const loadGovernance = async () => {
    if (!inspection) return;
    const current = generation.current;
    setBusy(true);
    setError('');
    try {
      const [agreement, restrictions] = await Promise.all([
        sessionApi.candidateConsensus(organizationId, inspection.queue_sha256),
        sessionApi.candidateSourceRestrictions(organizationId, inspection.queue_sha256),
      ]);
      if (current !== generation.current) return;
      if (agreement.independent_gold || agreement.training_approved || agreement.rights_verified
        || restrictions.training_approved || restrictions.rights_verified) {
        throw new Error('История проверок не даёт разрешения на обучение модели и не подтверждает права на данные.');
      }
      setConsensus(agreement);
      setSourceHistory(restrictions);
    } catch (cause) {
      if (current === generation.current) {
        setError(cause instanceof Error ? cause.message : 'Согласование недоступно.');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const submitSource = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!inspection || !archive || !csrf || !sourceConfirmed
      || !sourceOptions.includes(sourceSha) || sourceReason.trim().length < 32
      || sourceReason.trim().length > 1000 || busy) return;
    const current = generation.current;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const saved = await sessionApi.attestCandidateSourceRestriction(
        organizationId, archive, csrf, inspection, sourceSha, sourceKind,
        sourceFinding, null, sourceReason,
      );
      if (current !== generation.current) return;
      if (!saved.recorded || saved.training_approved || saved.rights_verified
        || saved.source_sha256 !== sourceSha || saved.finding !== sourceFinding) {
        throw new Error('Не удалось подтвердить проверку ограничений для выбранного файла.');
      }
      setSourceConfirmed(false);
      setSourceReason('');
      setSourceHistory(null);
      setNotice('Ограничение использования исходного документа сохранено. Это не подтверждение прав или разрешение на обучение.');
    } catch (cause) {
      if (current === generation.current) {
        setError(cause instanceof Error ? cause.message : 'Аудит источника недоступен.');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  return <section className="access-catalog-state" aria-labelledby="candidate-review-title">
    <h2 id="candidate-review-title">Проверка соответствия товаров заявке</h2>
    <p className="access-hint">
      Загрузите защищённый ZIP с dataset.json, split.json, catalog.json,
      queue.json и исходными файлами sources/. Результаты — только личные
      заключения, не обучающие метки и не разрешение использовать исходники.
    </p>
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <div className="alert success" role="status">{notice}</div>}
    <form className="access-form" onSubmit={event => {
      event.preventDefault(); void inspect();
    }}>
      <label>Оригиналы заявок и очередь товаров (ZIP)
        <input type="file" accept=".zip,application/zip" disabled={busy}
          onChange={event => chooseArchive(event.target.files?.[0] ?? null)}/>
      </label>
      <button className="button button-white" type="submit"
        disabled={!archive || !csrf || busy}>Проверить и открыть очередь</button>
    </form>
    {inspection && <>
      <p className="access-hint">
        Запросов: {inspection.total_requests}. Очередь SHA-256:
        {' '}<code>{inspection.queue_sha256}</code>.
        Права и приватность источников ещё требуют отдельной проверки.
      </p>
      <ul className="tenant-list">
        {inspection.tasks.map(task => <li key={task.request_id}>
          <span>
            <strong>{task.request_id}</strong> — {task.request_text}
            <small>{task.partition} · {task.candidates.length} предложенных товаров</small>
          </span>
          <button type="button" className="button button-white" disabled={busy}
            onClick={() => chooseTask(task)}>Оценить товары</button>
        </li>)}
      </ul>
      <div className="supplier-pagination">
        <button type="button" className="button button-white"
          disabled={busy || inspection.offset === 0}
          onClick={() => void inspect(Math.max(0, inspection.offset - 25))}>
          Предыдущие заявки
        </button>
        <span>Позиции {inspection.tasks.length ? inspection.offset + 1 : 0}–
          {inspection.offset + inspection.tasks.length} из {inspection.total_requests}</span>
        <button type="button" className="button button-white"
          disabled={busy || inspection.next_offset === null}
          onClick={() => {
            if (inspection.next_offset !== null) void inspect(inspection.next_offset);
          }}>Следующие заявки</button>
      </div>
      <section className="supplier-decision" aria-label="Проверка ограничений исходных заявок">
        <h3>Права на исходные документы и защита данных</h3>
        <p className="access-hint">
          Отдельный журнал ограничений. Здесь нельзя подтвердить лицензию,
          отсутствие персональных данных или разрешение на обучение модели.
        </p>
        <form className="access-form" onSubmit={event => void submitSource(event)}>
          <label>SHA-256 исходного документа
            <select value={sourceSha} disabled={busy} required
              onChange={event => {
                setSourceSha(event.target.value); setSourceConfirmed(false);
              }}>
              <option value="">Выберите исходник</option>
              {sourceOptions.map(sha => <option value={sha} key={sha}>{sha}</option>)}
            </select>
          </label>
          <label>Область проверки
            <select value={sourceKind} disabled={busy}
              onChange={event => {
                setSourceKind(event.target.value as 'rights' | 'privacy');
                setSourceConfirmed(false);
              }}>
              <option value="rights">Права на использование</option>
              <option value="privacy">Приватность данных</option>
            </select>
          </label>
          <label>Ограничение
            <select value={sourceFinding} disabled={busy}
              onChange={event => {
                setSourceFinding(event.target.value as SourceRestrictionFinding);
                setSourceConfirmed(false);
              }}>
              <option value="requires_evidence">Нужны независимые доказательства</option>
              <option value="restricted">Использование ограничено</option>
              <option value="rejected">Источник отклонён</option>
            </select>
          </label>
          <label>Обоснование ограничения по оригиналу
            <textarea rows={3} required minLength={32} maxLength={1000}
              disabled={busy} value={sourceReason}
              onChange={event => {
                setSourceReason(event.target.value); setSourceConfirmed(false);
              }}/>
          </label>
          <label className="tenant-checkbox">
            <input type="checkbox" checked={sourceConfirmed} disabled={busy}
              onChange={event => setSourceConfirmed(event.target.checked)}/>
            Я лично проверил исходный документ и фиксирую ограничение,
            а не разрешение использовать его для обучения.
          </label>
          <button type="submit" className="button button-white" disabled={
            busy || !csrf || !sourceConfirmed || !sourceOptions.includes(sourceSha)
            || sourceReason.trim().length < 32 || sourceReason.trim().length > 1000
          }>Записать ограничение исходника</button>
        </form>
      </section>
      <button type="button" className="button button-white" disabled={busy}
        onClick={() => void loadGovernance()}>
        Согласование оценок и ограничения использования
      </button>
      {consensus && <>
        <p className="access-hint">
          Совпали оценки двух проверяющих сотрудников: {consensus.agreed.length}.
          Конфликты: {consensus.conflicts.length}.
          Недостаточно проверок или есть неопределённость: {consensus.incomplete.length}.
          Обучение модели запрещено.
        </p>
        <ul className="tenant-list">
          {consensus.conflicts.map(item => <li key={item.task_sha256}>
            Конфликт по заявке {item.request_id} — {item.reviewer_count} рецензентов
          </li>)}
        </ul>
      </>}
      {canAdjudicate && consensus && inspection && archive && csrf && <TenantCandidateAdjudication
        key={inspection.queue_sha256} organizationId={organizationId} csrf={csrf}
        archive={archive} inspection={inspection} consensus={consensus}
      />}
      {sourceHistory && <>
        <p className="access-hint">Зафиксировано ограничений: {sourceHistory.rows.length}.</p>
        <ul className="tenant-list">
          {sourceHistory.rows.map(item => <li
            key={item.source_sha256 + item.review_kind + item.reviewer_user_id}>
            <span>
              {item.review_kind} — {item.finding}
              <small>{item.source_sha256} · {item.reviewer_user_id}</small>
              <small>{item.rationale}</small>
            </span>
          </li>)}
        </ul>
        {sourceHistory.next_offset !== null && <p className="access-hint">
          Показана первая страница журнала. Остальные записи доступны через API для технической проверки.
        </p>}
      </>}
      {selected && <form className="access-form" onSubmit={event => void submit(event)}>
        <h3>Заявка {selected.request_id}: независимая оценка</h3>
        <p>{selected.request_text}</p>
        {selected.candidates.map(product => <label key={product.sku}>
          {product.sku}: {product.name} ({product.sale_unit},
          {' '}{product.pieces_per_sale_unit} шт.)
          <select value={judgments[product.sku] ?? ''} disabled={busy}
            onChange={event => {
              const value = event.target.value;
              setJudgments(old => {
                if (value === 'accepted' || value === 'rejected' || value === 'unresolved') {
                  return { ...old, [product.sku]: value };
                }
                const updated = { ...old };
                delete updated[product.sku];
                return updated;
              });
              setNoMatchChecked(false);
              setPersonal(false);
            }}>
            <option value="">Выберите решение</option>
            <option value="accepted">Соответствует заявке</option>
            <option value="rejected">Не соответствует</option>
            <option value="unresolved">Неясно, нужна проверка</option>
          </select>
        </label>)}
        {allRejected && <label className="tenant-checkbox">
          <input type="checkbox" checked={noMatchChecked} disabled={busy}
            onChange={event => setNoMatchChecked(event.target.checked)}/>
          Я проверил: ни один из показанных товаров не подходит.
          Это не утверждение об отсутствии подходящего товара во всём прайсе.
        </label>}
        <label>Обоснование оценки по оригинальной заявке
          <textarea required minLength={32} maxLength={1000} rows={3}
            value={rationale} disabled={busy}
            onChange={event => { setRationale(event.target.value); setPersonal(false); }}/>
        </label>
        <label className="tenant-checkbox">
          <input type="checkbox" checked={personal} disabled={busy}
            onChange={event => setPersonal(event.target.checked)}/>
          Я лично проверил оригинал заявки и каждый предложенный товар.
        </label>
        <button className="button button-primary" type="submit" disabled={!canSubmit}>
          {busy ? 'Запись…' : 'Записать личную оценку'}
        </button>
      </form>}
      <button type="button" className="button button-white" disabled={busy}
        onClick={() => void loadHistory()}>История проверок очереди</button>
      {history && <>
        <ul className="tenant-list">
          {history.rows.map(record => <li key={record.task_sha256 + record.reviewer_user_id}>
            <span><strong>{record.request_id}</strong>
              <small>{record.reviewer_user_id} · {
                new Date(record.recorded_at).toLocaleString('ru-RU')
              }</small>
              <small>{record.accepted_skus.length} соответствует,
                {' '}{record.rejected_skus.length} отклонено,
                {' '}{record.unresolved_skus.length} требуют уточнения</small>
              <small>{record.rationale}</small>
            </span>
          </li>)}
        </ul>
        {history.next_offset !== null && <button type="button" className="button button-white"
          disabled={busy}
          onClick={() => {
            if (history.next_offset !== null) void loadHistory(history.next_offset);
          }}>Следующая страница истории</button>}
        <p className="access-hint">История не подтверждает права на данные и не заменяет проверенный набор для обучения.</p>
        <small>Страница истории с позиции {historyOffset + 1}</small>
      </>}
    </>}
  </section>;
}
