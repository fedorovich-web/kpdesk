import { useEffect, useState } from 'react';
import { designAssets } from '../design-assets';
import '../styles/proposal-share.css';

export function SharedProposalPage() {
  const [token, setToken] = useState(() => window.location.hash.slice(1));
  const [pdf, setPdf] = useState('');
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const changed = () => setToken(window.location.hash.slice(1));
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    let objectUrl = '';
    setPdf(''); setError('');
    if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
      setError('Ссылка недоступна. Обратитесь к отправителю КП.');
      return () => { cancelled = true; };
    }
    const timeout = setTimeout(() => controller.abort(), 30_000);
    (async () => {
      try {
        const response = await fetch('/api/public/proposal/pdf', {
          method: 'POST', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
          headers: { 'X-Proposal-Token': token }, signal: controller.signal,
        });
        if (response.status === 404) throw new Error('unavailable');
        if (!response.ok || !response.headers.get('content-type')?.startsWith('application/pdf')) throw new Error('temporary');
        const blob = await response.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob); setPdf(objectUrl);
      } catch (failure) {
        if (!cancelled) setError(failure instanceof Error && failure.message === 'unavailable'
          ? 'Ссылка недоступна. Обратитесь к отправителю КП.'
          : 'Не удалось загрузить КП. Проверьте соединение и попробуйте ещё раз.');
      } finally { clearTimeout(timeout); }
    })();
    return () => { cancelled = true; controller.abort(); clearTimeout(timeout);
      if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [attempt, token]);
  return <div className="shared-proposal-page">
    <header className="shared-proposal-shell"><a href="/" aria-label="KPDesk — главная" referrerPolicy="no-referrer">
      <img src={designAssets.logo} alt="KPDesk" width={203} height={52}/></a></header>
    <main className="shared-proposal-shell">
      <h1>Коммерческое предложение</h1>
      {!pdf && !error && <p role="status">Открываем утверждённый документ…</p>}
      {error && <><p role="alert">{error}</p>
        {!error.startsWith('Ссылка недоступна') && <button type="button" className="button button-primary"
          onClick={() => setAttempt(value => value + 1)}>Повторить загрузку</button>}</>}
      {pdf && <><p>Отправитель утвердил эту версию предложения. Откройте документ или скачайте PDF.</p>
        <div className="quote-share-actions"><a className="button button-primary" href={pdf} download="KPDesk-proposal.pdf">Скачать PDF</a>
          <a className="button button-white" href={pdf} target="_blank" rel="noopener noreferrer">Открыть PDF</a></div>
        <object className="shared-proposal-pdf" data={pdf + '#view=FitH'} type="application/pdf" aria-label="Утверждённое КП">
          <p>Если просмотр PDF недоступен в браузере, скачайте документ кнопкой выше.</p></object></>}
    </main>
    <footer className="shared-proposal-shell">KPDesk · Коммерческое предложение</footer>
  </div>;
}
