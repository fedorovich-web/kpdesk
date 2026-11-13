import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type { PartyDetails, PartyLookupResponse } from '../tenant-session';

export function formatPartyDetails(party: PartyDetails): string {
  return [party.full_name, `ИНН: ${party.inn}`, party.kpp && `КПП: ${party.kpp}`,
    party.ogrn && `ОГРН: ${party.ogrn}`, party.address && `Адрес: ${party.address}`]
    .filter(Boolean).join('\n');
}

export function CompanyInnLookup({ organizationId, csrf, disabled = false, onApply }: {
  organizationId: string; csrf: string | null; disabled?: boolean;
  onApply: (party: PartyDetails) => void;
}) {
  const [inn, setInn] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<PartyLookupResponse | null>(null);
  const epoch = useRef(0);
  const scope = JSON.stringify([organizationId, csrf, disabled, inn]);
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const resultScope = useRef('');
  useEffect(() => {
    epoch.current++;
    setBusy(false); setResult(null); setError('');
    return () => { epoch.current++; };
  }, [organizationId, csrf, disabled]);
  const find = async () => {
    if (!csrf || disabled || busy) return;
    setResult(null); setError('');
    if (!/^(?:[0-9]{10}|[0-9]{12})$/.test(inn)) {
      setError('Укажите ИНН: 10 или 12 цифр от 0 до 9.'); return;
    }
    const sentScope = scopeRef.current;
    const version = ++epoch.current;
    setBusy(true);
    try {
      const response = await sessionApi.lookupCompany(organizationId, inn, csrf);
      if (version !== epoch.current || sentScope !== scopeRef.current) return;
      resultScope.current = sentScope;
      setResult(response);
    } catch (cause) {
      if (version === epoch.current && sentScope === scopeRef.current)
        setError(cause instanceof Error ? cause.message : 'Не удалось получить реквизиты компании.');
    } finally {
      if (version === epoch.current && sentScope === scopeRef.current) setBusy(false);
    }
  };
  const current = resultScope.current === scope ? result : null;
  const party = current?.party;
  return <div className="company-inn-lookup">
    <p>Введите ИНН, проверьте найденную компанию и добавьте её реквизиты.</p>
    <label>ИНН компании<input value={inn} inputMode="numeric" autoComplete="off" maxLength={12}
      disabled={disabled || !csrf} onChange={event => {
        epoch.current++; setInn(event.target.value); setResult(null); setError(''); setBusy(false);
      }}/></label>
    <button type="button" className="button button-white" disabled={disabled || !csrf || busy}
      onClick={() => void find()}>{busy ? 'Ищем реквизиты…' : 'Найти по ИНН'}</button>
    {busy && <p role="status">Запрашиваем реквизиты компании…</p>}
    {error && <p role="alert">{error}</p>}
    {current && <div className="company-inn-preview">
      <p>Источник: <a href="https://dadata.ru/api/find-party/" target="_blank" rel="noreferrer">DaData</a>.
        {' '}Получено: <time dateTime={current.fetched_at}>{new Date(current.fetched_at).toLocaleString('ru-RU')}</time>
        {current.cached && ' (из кеша)'}.</p>
      {!party ? <p role="status">Компания по этому ИНН не найдена.</p> : <>
        <dl className="company-inn-summary">
          <div><dt>Компания</dt><dd>{party.short_name}</dd></div>
          <div><dt>ИНН</dt><dd>{party.inn}</dd></div>
          <div><dt>Юридический адрес</dt><dd>{party.address ?? 'Не указано'}</dd></div>
        </dl>
        <details><summary>Реквизиты и статус в реестре</summary>
          <dl className="company-inn-details">
            {([['full_name', 'Полное наименование'], ['kpp', 'КПП'], ['ogrn', 'ОГРН / ОГРНИП'],
              ['status', 'Статус в реестре']] as const).map(([field, label]) =>
              <div key={field}><dt>{label}</dt><dd>{party[field] ?? 'Не указано'}</dd></div>)}
          </dl>
        </details>
        {party.status !== 'ACTIVE' && <p role="alert">Статус в реестре: {party.status ?? 'не указан'}.
          {' '}Проверьте актуальность и возможность работы с компанией перед использованием реквизитов.</p>}
        <p>Данные из реестра требуют вашей проверки и не подтверждают надёжность компании.</p>
        <button type="button" className="button button-white" disabled={disabled || !csrf || busy}
          onClick={() => { if (resultScope.current === scopeRef.current) onApply(party); }}>Добавить реквизиты</button>
      </>}
    </div>}
  </div>;
}
