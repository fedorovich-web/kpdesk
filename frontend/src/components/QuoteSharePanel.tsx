import { useEffect, useRef, useState } from 'react';
import { sessionApi, SessionApiError } from '../tenant-session';
import type { QuoteShare, TenantQuote } from '../tenant-session';

type Props = { organizationId: string; quote: TenantQuote; csrf: string | null; canWrite: boolean };
const active = (share: QuoteShare) => !share.revoked_at && Date.parse(share.expires_at) > Date.now();
const date = (value: string) => new Date(value).toLocaleString('ru-RU');

export function QuoteSharePanel(props: Props) {
  return <SharePanel key={JSON.stringify([props.organizationId, props.quote.quote_id,
    props.quote.revision, props.quote.snapshot_sha256, props.csrf, props.canWrite])} {...props}/>;
}

function SharePanel({ organizationId, quote, csrf, canWrite }: Props) {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [shares, setShares] = useState<QuoteShare[]>([]);
  const [issued, setIssued] = useState<{ id: string; url: string } | null>(null);
  const [days, setDays] = useState(7);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(false);
  const operation = useRef(0);

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    sessionApi.quoteShares(organizationId, quote.quote_id).then(result => {
      if (!cancelled) { setAvailable(result.available); setShares(result.shares); }
    }).catch(() => { if (!cancelled) setError('Не удалось загрузить ссылки для клиента.'); });
    return () => { cancelled = true; alive.current = false; operation.current++; };
  }, [organizationId, quote.quote_id]);

  async function refresh() {
    const version = ++operation.current; setPending(true); setError('');
    try {
      const result = await sessionApi.quoteShares(organizationId, quote.quote_id);
      if (!alive.current || version !== operation.current) return;
      setAvailable(result.available); setShares(result.shares);
      if (!result.shares.some(active)) setUncertain(false);
    } catch { if (alive.current) setError('Не удалось обновить список ссылок.'); }
    finally { if (alive.current && version === operation.current) setPending(false); }
  }
  async function create() {
    if (!csrf || pending || uncertain) return;
    const version = ++operation.current; setPending(true); setError(''); setNotice(''); setIssued(null);
    try {
      const result = await sessionApi.createQuoteShare(organizationId, quote, days, csrf);
      if (!alive.current || version !== operation.current) return;
      if (!/^[A-Za-z0-9_-]{43}$/.test(result.token) || result.share.revision !== quote.revision) {
        throw new Error('invalid_response');
      }
      setIssued({ id: result.share.id, url: window.location.origin + '/proposal#' + result.token });
      setShares(current => [result.share, ...current]);
      setNotice('Ссылка создана. Скопируйте её: после закрытия страницы адрес восстановить нельзя.');
    } catch (failure) {
      if (!alive.current || version !== operation.current) return;
      const unknown = !(failure instanceof SessionApiError) || failure.status === 0 || failure.status >= 500
        || failure.code === 'invalid_response' || (failure.status >= 200 && failure.status < 300);
      setUncertain(unknown);
      setError(unknown ? 'Не удалось подтвердить создание ссылки. Обновите список и отзовите новую ссылку, прежде чем создавать другую.'
        : failure.message);
    } finally { if (alive.current && version === operation.current) setPending(false); }
  }
  async function revoke(share: QuoteShare) {
    if (!csrf || pending) return;
    const version = ++operation.current; setPending(true); setError('');
    try {
      await sessionApi.revokeQuoteShare(organizationId, quote.quote_id, share.id, csrf);
      if (!alive.current || version !== operation.current) return;
      const next = shares.map(item => item.id === share.id ? { ...item, revoked_at: new Date().toISOString() } : item);
      setShares(next); if (issued?.id === share.id) setIssued(null);
      if (!next.some(active)) setUncertain(false);
      setNotice('Доступ по ссылке отозван. Уже скачанные файлы остаются у получателя.');
    } catch { if (alive.current) setError('Отзыв не подтверждён. Обновите список и проверьте статус ссылки.'); }
    finally { if (alive.current && version === operation.current) setPending(false); }
  }
  async function copy() {
    if (!issued) return;
    try { await navigator.clipboard.writeText(issued.url); if (alive.current) setNotice('Ссылка скопирована.'); }
    catch { if (alive.current) setError('Не удалось скопировать. Выделите адрес в поле и скопируйте вручную.'); }
  }

  if (available === false || (available === null && !error)) return null;
  return <section className="quote-share-panel" aria-label="Клиентские ссылки">
    <h3>Ссылка для клиента</h3>
    <p>Каждая ссылка открывает утверждённый PDF указанной в списке версии. Любой, у кого есть ссылка,
      сможет открыть документ до истечения срока или отзыва. Уже скачанный файл останется у получателя.</p>
    {canWrite && available && quote.approved && <div className="quote-share-actions">
      <label>Срок доступа<select value={days} disabled={pending} onChange={event => setDays(Number(event.target.value))}>
        <option value={1}>1 день</option><option value={7}>7 дней</option><option value={30}>30 дней</option>
      </select></label>
      <button type="button" className="button button-primary" disabled={pending || uncertain || !csrf}
        onClick={() => void create()}>Создать ссылку для клиента</button>
    </div>}
    {!quote.approved && <p>Новая ссылка доступна после утверждения текущей версии.
      Ранее выданные ссылки можно отозвать ниже.</p>}
    <button type="button" className="button button-white" disabled={pending} onClick={() => void refresh()}>
      Обновить список ссылок</button>
    {pending && <p role="status">Обновляем ссылки…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {issued && <div className="quote-share-address"><label>Ссылка для клиента<input readOnly value={issued.url}
      onFocus={event => event.currentTarget.select()}/></label>
      <button type="button" className="button button-white" onClick={() => void copy()}>Копировать ссылку</button></div>}
    {shares.length > 0 && <ul>{shares.map(share => <li key={share.id}>
      <span>Версия {share.revision} · создана {date(share.created_at)} · до {date(share.expires_at)}
        {' · '}<strong>{share.revoked_at ? 'Отозвана' : active(share) ? 'Действует' : 'Срок истёк'}</strong></span>
      {canWrite && active(share) && <button type="button" className="button button-white" disabled={pending || !csrf}
        onClick={() => void revoke(share)} aria-label={'Отозвать ссылку от ' + date(share.created_at)}>Отозвать ссылку</button>}
    </li>)}</ul>}
  </section>;
}
