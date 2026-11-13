import { useLayoutEffect, useRef, useState } from 'react';
import { money } from '../domain';
import type { ConfirmedInstructions } from '../domain';
import { sessionApi } from '../tenant-session';
import type { CompositionVariant, TenantVariantsResponse } from '../tenant-session';

type Props = {
  organizationId: string; catalogId: string; csrf: string; identity: string;
  requestedItems: object[]; instructions?: ConfirmedInstructions; enabled: boolean;
  onApply: (response: TenantVariantsResponse, variant: CompositionVariant) => void;
};

function transferable(variant: CompositionVariant, result: TenantVariantsResponse): boolean {
  return variant.selectable && !variant.budget_exceeded && variant.total_rub !== null
    && !variant.unresolved_line_numbers.length && variant.lines.length === result.lines.length
    && variant.lines.every((line, index) => line.line_number === index + 1
      && result.lines[index]?.candidates.some(c => c.sku === line.sku && c.status === 'compatible'));
}

export function CompositionVariants(props: Props) {
  const { organizationId, catalogId, csrf, identity, requestedItems, instructions, enabled, onApply } = props;
  const [result, setResult] = useState<TenantVariantsResponse | null>(null);
  const [reviewed, setReviewed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const epoch = useRef(0);
  useLayoutEffect(() => {
    epoch.current++;
    setResult(null); setReviewed(null); setBusy(false); setError('');
    return () => { epoch.current++; };
  }, [identity, organizationId, catalogId, csrf, enabled]);

  const compare = async () => {
    if (!enabled || busy) return;
    const current = ++epoch.current;
    setBusy(true); setError(''); setResult(null); setReviewed(null);
    try {
      const preview = await sessionApi.previewVariants(
        organizationId, catalogId, requestedItems, csrf, instructions,
      );
      if (current !== epoch.current) return;
      if (preview.catalog_version_id !== catalogId || preview.mode !== 'tfidf'
        || preview.lines.length !== requestedItems.length
        || preview.lines.some((line, index) => line.line_number !== index + 1
          || line.catalog_version_id !== catalogId || line.mode !== 'tfidf'
          || !line.requires_manager_confirmation)) {
        throw new Error('Ответ не соответствует текущей заявке или каталогу. Повторите сравнение.');
      }
      setResult(preview);
    } catch (failure) {
      if (current === epoch.current) setError(failure instanceof Error ? failure.message : 'Сравнение недоступно.');
    } finally {
      if (current === epoch.current) setBusy(false);
    }
  };

  return <section className="composition-comparison" aria-label="Варианты комплектации">
    <div className="tenant-actions">
      <button type="button" className="button button-white" disabled={!enabled || busy}
        onClick={() => { void compare(); }}>Сравнить варианты комплектации</button>
      {busy && <span role="status">Сравниваем состав и стоимость…</span>}
    </div>
    <p>Сравните два варианта: товары по результату подбора и товары с меньшей стоимостью.
      Оба учитывают характеристики, бренды и подтверждённую наценку.</p>
    {error && <p role="alert" className="alert error">{error}</p>}
    {result && <>
      <p className="composition-scope">Сравниваются до {result.candidate_limit_per_line} найденных
        товаров на позицию. Это не поиск самой низкой цены во всём прайсе.
        Наличие и сроки поставки требуют отдельного подтверждения.</p>
      {result.variants.length === 1 && <p>Получился один состав: второй способ не дал отличий.</p>}
      <div className="composition-grid">
        {result.variants.map(variant => <article className="composition-card" key={variant.kind}>
          <h3>{variant.kind === 'relevance' ? 'По результату подбора' : 'Меньшая стоимость'}</h3>
          <p>{variant.kind === 'relevance'
            ? 'Для каждой позиции выбран первый подходящий товар из результатов подбора.'
            : 'Для каждой позиции выбран товар с меньшей стоимостью среди найденных вариантов.'}</p>
          <ol className="composition-lines">
            {variant.lines.map(line => <li key={line.line_number}>
              <span className="composition-line-label">Позиция {line.line_number} · {line.sku}</span>
              <strong>{line.name}</strong>
              <span>{line.sale_quantity} {line.sale_unit} · {money(line.line_total_rub)}</span>
            </li>)}
          </ol>
          {variant.unresolved_line_numbers.length > 0 && <p className="alert warning">
            Требуют уточнения позиции: {variant.unresolved_line_numbers.join(', ')}.
            Не найден товар, который соответствует требованиям и условиям продажи.</p>}
          <p className="composition-total">Итого: {variant.total_rub === null
            ? 'не рассчитано' : money(variant.total_rub)}</p>
          {variant.total_rub === null && !variant.unresolved_line_numbers.length
            && <p role="alert">Сумма превышает допустимый предел расчёта.</p>}
          {variant.budget_exceeded && <p className="alert warning">Превышен подтверждённый бюджет.
            Уточните состав или измените и заново подтвердите правила.</p>}
          {transferable(variant, result) && <>
            <label className="tenant-checkbox"><input type="checkbox"
              checked={reviewed === variant.kind} onChange={event => setReviewed(event.target.checked ? variant.kind : null)}/>
              Проверил состав, количество, упаковку и стоимость этого варианта
            </label>
            <button type="button" className="button button-primary" disabled={!enabled || reviewed !== variant.kind}
              onClick={() => { if (enabled && reviewed === variant.kind) onApply(result, variant); }}>
              Использовать этот вариант</button>
          </>}
        </article>)}
      </div>
    </>}
  </section>;
}
