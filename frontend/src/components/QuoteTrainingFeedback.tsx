import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type { TenantQuote, TrainingFeedbackContext, TrainingFeedbackDecision } from '../tenant-session';
import { RequirementMatrix } from './RequestAnalysis';
import { QuoteTrainingFeedbackHistory } from './QuoteTrainingFeedbackHistory';

type Props = { organizationId: string; quote: TenantQuote; csrf: string | null; canManage: boolean };
type Decision = { choice: string; rejected: string[] };

export function QuoteTrainingFeedback({ organizationId, quote, csrf, canManage }: Props) {
  const [context, setContext] = useState<TrainingFeedbackContext | null>(null);
  const [loadedScope, setLoadedScope] = useState('');
  const [decisions, setDecisions] = useState<Record<number, Decision>>({});
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [historyVersion,setHistoryVersion]=useState(0);
  const saveRetry=useRef<{body:string;key:string} | null>(null);
  const mounted = useRef(true);
  const scope = JSON.stringify([organizationId, quote.quote_id, quote.revision, quote.snapshot_sha256, csrf, canManage]);
  const scopeRef = useRef({scope,epoch:0});
  if(scopeRef.current.scope!==scope) scopeRef.current={scope,epoch:scopeRef.current.epoch+1};
  const epoch=scopeRef.current.epoch;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    setContext(null); setLoadedScope(''); setDecisions({}); setConsent(false); setBusy(false); setError(''); setNotice('');
    saveRetry.current=null;
  }, [scope]);
  const visible = loadedScope === scope && context?.quote_id === quote.quote_id && context.source_revision === quote.revision
    && context.source_snapshot_sha256 === quote.snapshot_sha256 ? context : null;
  const fresh = (sent: number) => mounted.current && scopeRef.current.epoch === sent;
  const load = async () => {
    if (!canManage || !csrf || busy) return;
    const sent = epoch;
    setBusy(true); setError(''); setNotice(''); setContext(null); setDecisions({}); setConsent(false);
    try {
      const result = await sessionApi.trainingFeedbackContext(organizationId, quote, csrf);
      if (!fresh(sent)) return;
      if (result.quote_id !== quote.quote_id || result.source_revision !== quote.revision
        || result.source_snapshot_sha256 !== quote.snapshot_sha256 || result.catalog_version_id !== quote.catalog_version_id) {
        throw new Error('Версия КП изменилась. Откройте оценку подбора заново.');
      }
      setContext(result);
      setLoadedScope(scope);
    } catch (cause) { if (fresh(sent)) setError(cause instanceof Error ? cause.message : 'Не удалось загрузить товары для оценки.'); }
    finally { if (fresh(sent)) setBusy(false); }
  };
  const complete = !!visible?.lines.length && visible.lines.every(line => {
    const value = decisions[line.line_number];
    return value && value.choice && value.rejected.length > 0
      && (value.choice !== '__no_match__' || (line.no_match_allowed
        && line.candidates.every(candidate => value.rejected.includes(candidate.sku))));
  });
  const download = async (save=false) => {
    if (!complete || !consent || !visible || !csrf || !canManage || busy) return;
    const sent = epoch;
    const rows: TrainingFeedbackDecision[] = [];
    for (const line of visible.lines) {
      const value = decisions[line.line_number];
      if (!value) return;
      rows.push({ line_number: line.line_number,
        relevant_sku: value.choice === '__no_match__' ? null : value.choice,
        no_match: value.choice === '__no_match__', rejected_skus: value.rejected });
    }
    setBusy(true); setError(''); setNotice('');
    try {
      if(save){
        const body=JSON.stringify([scope,rows]);
        if(saveRetry.current?.body!==body) saveRetry.current={body,key:crypto.randomUUID()};
        const result=await sessionApi.saveTrainingFeedback(organizationId,quote,rows,consent,consent,csrf,saveRetry.current.key);
        if(!fresh(sent)) return;
        if(result.quote_id!==quote.quote_id || result.revision!==quote.revision || result.withdrawn_at)
          throw Error('Получены другие оценки. Обновите КП.');
        setConsent(false);saveRetry.current=null;setHistoryVersion(value=>value+1);
        setNotice('Оценки сохранены. Для обучения нужно отдельно подготовить данные; действующий подбор не изменился.');
        return;
      }
      const result = await sessionApi.exportTrainingFeedback(organizationId, quote, rows, consent, consent, csrf);
      if (!fresh(sent)) return;
      if (result.format !== 'kpdesk-manager-feedback-v1' || result.training_data_kind !== 'manager_reviewed'
        || result.organization_id !== organizationId || result.quote_id !== quote.quote_id
        || result.revision !== quote.revision || result.snapshot_sha256 !== quote.snapshot_sha256
        || result.catalog_version_id !== quote.catalog_version_id || !result.training_consent) {
        throw new Error('Оценки относятся к другой версии. Обновите КП.');
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(result)], { type: 'application/json' }));
      const anchor = document.createElement('a');
      try {
        anchor.href = url; anchor.download = 'kpdesk-manager-feedback.json';
        document.body.append(anchor); anchor.click();
      } finally { anchor.remove(); URL.revokeObjectURL(url); }
      setConsent(false);
      setNotice('Файл с оценками скачан. Его можно использовать для отдельной подготовки обучающих данных; действующая модель подбора не изменена.');
    } catch (cause) { if (fresh(sent)) setError(cause instanceof Error ? cause.message : 'Не удалось скачать оценки.'); }
    finally { if (fresh(sent)) setBusy(false); }
  };
  if (!canManage) return null;
  return <details className="access-catalog-state training-feedback" aria-label="Обратная связь для улучшения подбора">
    <summary>Оценить подбор товаров</summary>
    <p>Оцените соответствие товаров заявке. Учитывайте назначение и характеристики, а не цену или условия поставки.
      Непросмотренные товары не будут отмечены как неправильные.</p>
    <p>Владелец организации может сохранить оценки в истории или скачать файл для отдельного обучения.
      Автоматическое переобучение не запускается.</p>
    {error && <p role="alert" className="alert error">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <button type="button" className="button button-white" disabled={busy || !csrf || !quote.source_request?.lines.length}
      onClick={() => void load()}>Загрузить товары для оценки</button>
    {!quote.source_request?.lines.length && <p>Чтобы оценить подбор, нужна сохранённая исходная заявка.</p>}
    {visible && <div>
      {visible.lines.map(line => <fieldset key={line.line_number} data-selected-sku={line.selected_sku ?? ''} disabled={busy}>
        <legend>Строка {line.line_number}: {line.description}</legend>
        <p>В КП выбран: {line.candidates.find(item => item.sku === line.selected_sku)?.name ?? 'товар не выбран'}.</p>
        <label>Подходящий товар · строка {line.line_number}
          <select value={decisions[line.line_number]?.choice ?? ''} onChange={event => {
            const choice = event.target.value;
            setDecisions(current => ({ ...current, [line.line_number]: { choice,
              rejected: (current[line.line_number]?.rejected ?? []).filter(sku => sku !== choice) } }));
            setConsent(false);
          }}>
            <option value="">Выберите после проверки</option>
            {line.candidates.map(candidate => <option key={candidate.sku} value={candidate.sku}>
              {candidate.name} ({candidate.sku})</option>)}
            {line.no_match_allowed && <option value="__no_match__">Подходящего товара нет в прайсе</option>}
          </select>
        </label>
        {line.candidates.find(candidate => candidate.sku === decisions[line.line_number]?.choice)?.requirements &&
          <RequirementMatrix summary="Характеристики выбранного товара"
            checks={line.candidates.find(candidate => candidate.sku === decisions[line.line_number]?.choice)?.requirements}/>}
        <p>Отметьте хотя бы один товар, который не подходит к заявке:</p>
        {line.candidates.filter(candidate => candidate.sku !== decisions[line.line_number]?.choice).map(candidate =>
          <div key={candidate.sku}><label className="checkbox-label">
            <input type="checkbox" checked={decisions[line.line_number]?.rejected.includes(candidate.sku) ?? false}
              onChange={event => {
                const checked = event.target.checked;
                setDecisions(current => {
                  const value = current[line.line_number] ?? { choice: '', rejected: [] };
                  return { ...current, [line.line_number]: { ...value, rejected: checked
                    ? [...value.rejected, candidate.sku] : value.rejected.filter(sku => sku !== candidate.sku) } };
                });
                setConsent(false);
              }}/>
            Не подходит: {candidate.name} ({candidate.sku})
          </label>
          {candidate.requirements && <RequirementMatrix summary={'Сравнить характеристики: ' + candidate.name}
            checks={candidate.requirements}/>}
          </div>)}
        {decisions[line.line_number]?.choice === '__no_match__' && <p>Для этого решения отметьте все товары прайса как неподходящие.</p>}
        {!line.no_match_allowed && <p>Показана часть прайса: по этой выборке нельзя подтвердить отсутствие товара во всём каталоге.</p>}
      </fieldset>)}
      <label className="checkbox-label"><input type="checkbox" checked={consent} disabled={busy || !complete}
        onChange={event => setConsent(event.target.checked)}/>
        Я проверил оценки и разрешаю использовать эти данные для обучения модели моей организации.</label>
      <button type="button" className="button" disabled={busy || !complete || !consent || !csrf}
        onClick={() => void download()}>Скачать оценки для обучения</button>
      {' '}<button type="button" className="button button-white" disabled={busy || !complete || !consent || !csrf}
        onClick={() => void download(true)}>Сохранить оценки в организации</button>
    </div>}
    <QuoteTrainingFeedbackHistory organizationId={organizationId} quoteId={quote.quote_id} csrf={csrf}
      scope={scope} version={historyVersion}/>
  </details>;
}
