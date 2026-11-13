import { useLayoutEffect, useRef, useState } from 'react';
import { money } from '../domain';
import { sessionApi } from '../tenant-session';
import type { DocumentAlternative, DocumentAlternativePreview, TenantQuote } from '../tenant-session';

export function DocumentAlternativeSummary({ alternative, title }: { alternative: DocumentAlternative; title: string }) {
  return <>
    <h4>{title}</h4>
    <ol className="composition-lines">{alternative.lines.map(line => <li key={line.line_number}>
      <span className="composition-line-label">Позиция {line.line_number} · {line.sku}</span>
      <strong>{line.product_name}</strong>
      <span>{line.sale_quantity} {line.sale_unit} · {money(line.line_total_rub ?? '')}</span>
    </li>)}</ol>
    <p className="composition-total">Итого: {money(alternative.total_rub)}</p>
  </>;
}

type Props = {
  organizationId: string; quote: TenantQuote; csrf: string | null; disabled: boolean;
  onChange: (alternatives: DocumentAlternative[]) => void;
};

export function QuoteDocumentAlternatives({ organizationId, quote, csrf, disabled, onChange }: Props) {
  const [preview, setPreview] = useState<DocumentAlternativePreview | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const epoch = useRef(0);
  const scope = JSON.stringify([organizationId, quote.quote_id, quote.revision, quote.snapshot_sha256, csrf]);
  useLayoutEffect(() => {
    epoch.current++; setPreview(null); setSelected([]); setBusy(false); setError('');
    return () => { epoch.current++; };
  }, [scope]);

  const load = async () => {
    if (!csrf || disabled || busy) return;
    const current = ++epoch.current;
    if (selected.length) onChange([]);
    setPreview(null); setSelected([]); setBusy(true); setError('');
    try {
      const result = await sessionApi.previewDocumentAlternatives(organizationId, quote, csrf);
      if (current !== epoch.current) return;
      if (result.quote_id !== quote.quote_id || result.source_revision !== quote.revision
        || result.source_snapshot_sha256 !== quote.snapshot_sha256
        || result.alternatives.length > 2
        || result.alternatives.some(item => item.catalog_version_id !== quote.catalog_version_id)) {
        throw new Error('Варианты относятся к другой версии. Обновите КП.');
      }
      setPreview(result);
    } catch (failure) {
      if (current === epoch.current) setError(failure instanceof Error ? failure.message : 'Не удалось сравнить составы.');
    } finally {
      if (current === epoch.current) setBusy(false);
    }
  };
  return <section className="composition-comparison" aria-label="Дополнительные варианты в клиентском КП">
    <h4>Предложить клиенту выбор</h4>
    <p>Добавьте в предложение до двух вариантов товаров из того же прайса.
      При генерации текста ИИ учтёт выбранные варианты. Каждый вариант будет показан в PDF с отдельной суммой.</p>
    <button type="button" className="button button-white" disabled={!csrf || disabled || busy}
      onClick={() => { void load(); }}>{busy ? 'Подбираем варианты…' : 'Подобрать другие варианты для клиента'}</button>
    {error && <p role="alert">{error}</p>}
    {preview && <>
      <p>Сравниваются до {preview.candidate_limit_per_line} найденных товаров на позицию.
        Цена не подтверждает качество, наличие или срок поставки. Суммы вариантов не складываются.</p>
      {!preview.alternatives.length && <p role="status">Не удалось подобрать дополнительные варианты
        с соблюдением требований и бюджета. Можно продолжить с основным вариантом.</p>}
      <div className="composition-grid">{preview.alternatives.map((alternative, index) =>
        <article className="composition-card" key={index}>
          <DocumentAlternativeSummary alternative={alternative} title={`Дополнительный вариант ${index + 1}`}/>
          <label className="tenant-checkbox"><input type="checkbox" disabled={disabled || busy}
            checked={selected.includes(index)} onChange={event => {
              const next = event.target.checked ? [...selected, index].sort() : selected.filter(value => value !== index);
              setSelected(next); onChange(next.map(value => preview.alternatives[value]!));
            }}/>
            Добавить этот вариант в КП
          </label>
        </article>)}</div>
    </>}
  </section>;
}
