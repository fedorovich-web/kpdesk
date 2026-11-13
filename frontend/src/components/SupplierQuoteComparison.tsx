import { useEffect, useRef, useState } from 'react';
import { money } from '../domain';
import type { ConfirmedInstructions } from '../domain';
import { sessionApi } from '../tenant-session';
import type {
  QuoteLineInput, SupplierQuotePreview,
  SupplierSelection, SupplierSourceRef, TenantQuote,
} from '../tenant-session';

type Props = {
  organizationId: string; csrf: string; activeVersionId: string;
  requestLines: QuoteLineInput[]; instructions?: ConfirmedInstructions;
  onSaved: (quote: TenantQuote) => void;
};
type SourceOption = { ref: SupplierSourceRef; label: string };
type SupplierChoice = { supplier: string; options: SourceOption[]; chosen: string };
const signature = (source: SupplierSourceRef) =>
  source.profile_fingerprint + '|' + source.catalog_version_id;

export function SupplierQuoteComparison({
  organizationId, csrf, activeVersionId, requestLines, instructions, onSaved,
}: Props) {
  const [suppliers, setSuppliers] = useState<SupplierChoice[]>([
    { supplier: '', options: [], chosen: '' },
    { supplier: '', options: [], chosen: '' },
  ]);
  const [preview, setPreview] = useState<SupplierQuotePreview | null>(null);
  const [selections, setSelections] = useState<Record<number, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current++; };
  }, []);
  const invalidate = (next: SupplierChoice[]) => {
    generation.current++;
    setSuppliers(next);
    setPreview(null);
    setSelections({});
    setNotice('');
    setError('');
    setBusy(false);
  };
  const choice = (index: number, supplier: string) =>
    invalidate(suppliers.map((item, n) => n === index
      ? { supplier, options: [], chosen: '' } : item));

  const selectedSources = (): SupplierSourceRef[] | null => {
    const refs = suppliers.map(item =>
      item.options.find(option => signature(option.ref) === item.chosen)?.ref);
    if (refs.some(ref => ref === undefined) ||
      new Set(refs.map(ref => ref!.supplier_id)).size !== refs.length ||
      new Set(refs.map(ref => ref!.catalog_version_id)).size !== refs.length) return null;
    return refs as SupplierSourceRef[];
  };

  const findSources = async () => {
    const names = suppliers.map(item => item.supplier.trim());
    if (names.some(name => !/^[A-Za-z0-9_.-]{1,120}$/.test(name))
      || new Set(names).size !== names.length) {
      setError('Укажите от 2 до 10 разных идентификаторов проверенных поставщиков.');
      return;
    }
    const generationAtStart = ++generation.current;
    setBusy(true);
    setError('');
    setNotice('');
    setPreview(null);
    try {
      const result = await sessionApi.discoverSupplierSources(organizationId, names, csrf);
      if (!mounted.current || generation.current !== generationAtStart) return;
      if (result.suppliers.length !== names.length ||
        result.suppliers.some((group, index) => group.supplier_id !== names[index] ||
          group.options.some(option => option.supplier_id !== group.supplier_id))) {
        throw new Error('Некорректный ответ реестра подтверждённых прайсов.');
      }
      const resolved: SupplierChoice[] = result.suppliers.map(group => ({
        supplier: group.supplier_id, chosen: '',
        options: group.options.map(option => ({
          ref: {
            supplier_id: option.supplier_id,
            profile_fingerprint: option.profile_fingerprint,
            catalog_version_id: option.catalog_version_id,
          },
          label: (option.evidence === 'reviewed_canonical'
            ? 'Подготовленный прайс' : 'Исходный прайс')
            + ' · Версия ' + option.catalog_version_id.slice(0, 8)
            + ' · товаров: ' + option.rows_count + ' · источник ' + option.source_sha256.slice(0, 12),
        })),
      }));
      setSuppliers(resolved);
      if (resolved.some(item => item.options.length === 0)) {
        setError('Для одного или нескольких поставщиков нет проверенных '
          + 'опубликованных версий. Неподтверждённые цены исключены.');
      }
      if (result.suppliers.some(group => group.truncated)) {
        setNotice('Показана только часть истории прайсов. '
          + 'Более старые версии в этот список не вошли.');
      } else if (result.suppliers.some(group => !group.canonical_discovery_available)) {
        setNotice('Подготовленные прайсы пока недоступны. '
          + 'Показаны только доступные исходные прайсы.');
      }
    } catch (cause) {
      if (mounted.current && generation.current === generationAtStart) {
        setError(cause instanceof Error ? cause.message : 'Не удалось проверить источники.');
      }
    } finally {
      if (mounted.current && generation.current === generationAtStart) setBusy(false);
    }
  };

  const compare = async () => {
    const sources = selectedSources();
    if (!sources) { setError('Для каждого поставщика выберите проверенную версию прайса.'); return; }
    const generationAtStart = ++generation.current;
    setError(''); setBusy(true); setSelections({}); setPreview(null);
    try {
      const result = await sessionApi.previewSupplierQuote(
        organizationId, sources, requestLines, csrf, instructions,
      );
      if (!mounted.current || generation.current !== generationAtStart) return;
      if (!result.requires_manager_confirmation || result.catalog_source_count !== sources.length ||
        result.lines.length !== requestLines.length) {
        throw new Error('Ответ сравнения не соответствует запросу.');
      }
      setPreview(result);
    } catch (cause) {
      if (mounted.current && generation.current === generationAtStart) {
        setError(cause instanceof Error ? cause.message : 'Сравнение не выполнено.');
      }
    } finally {
      if (mounted.current && generation.current === generationAtStart) setBusy(false);
    }
  };

  const save = async () => {
    const sources = selectedSources();
    if (!sources || !preview || preview.lines.some(line =>
      selections[line.line_number] === undefined
      || !line.offers[selections[line.line_number]!]
    )) {
      setError('Выберите поставщика и товар для каждой позиции.');
      return;
    }
    const chosen: SupplierSelection[] = preview.lines.map(line => {
      const offer = line.offers[selections[line.line_number]!]!;
      return {
        line_number: line.line_number, supplier_id: offer.supplier_id,
        catalog_version_id: offer.catalog_version_id, sku: offer.sku,
      };
    });
    const generationAtStart = ++generation.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await sessionApi.createSupplierQuote(
        organizationId, activeVersionId, sources, requestLines, chosen,
        csrf, crypto.randomUUID(), instructions,
      );
      if (!mounted.current || generation.current !== generationAtStart) return;
      onSaved(saved);
      setNotice('КП сохранено как новая версия № ' + saved.revision +
        '. Источники цен зафиксированы по строкам.');
      setPreview(null);
      setSelections({});
    } catch (cause) {
      if (mounted.current && generation.current === generationAtStart) {
        setError(cause instanceof Error ? cause.message : 'Сохранение КП не выполнено.');
      }
    } finally {
      if (mounted.current && generation.current === generationAtStart) setBusy(false);
    }
  };

  const ready = preview && preview.lines.every(line =>
    selections[line.line_number] !== undefined
    && line.offers[selections[line.line_number]!] !== undefined);
  return <section className="access-catalog-state" aria-label="Сравнение поставщиков">
    <h3>Подбор по нескольким поставщикам</h3>
    <p>Сравните товары и стоимость по проверенным прайсам поставщиков.
      Для каждого поставщика укажите группу прайсов и выберите сохранённую версию.
      Цены и характеристики берутся из этих файлов. Выберите предложение для каждой позиции самостоятельно.</p>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {suppliers.map((item, index) => <div key={index} className="tenant-quote-rules">
      <label>Источник {index + 1} — ID группы прайсов
        <input maxLength={120} value={item.supplier} disabled={busy}
          onChange={event => choice(index, event.target.value)}
          placeholder="supplier_company_a"/>
      </label>
      {item.options.length > 0 && <label>Проверенный прайс поставщика {index + 1}
        <select value={item.chosen} disabled={busy} onChange={event => {
          invalidate(suppliers.map((source, n) =>
            n === index ? { ...source, chosen: event.target.value } : source));
        }}>
          <option value="">Выберите версию</option>
          {item.options.map(option => <option key={signature(option.ref)}
            value={signature(option.ref)}>{option.label}</option>)}
        </select>
      </label>}
      {suppliers.length > 2 && <button type="button" className="text-button"
        disabled={busy} onClick={() => invalidate(suppliers.filter((_, n) => n !== index))}>
        Убрать поставщика
      </button>}
    </div>)}
    <div className="tenant-ml-matching">
      {suppliers.length < 10 && <button type="button" className="button button-white"
        disabled={busy} onClick={() => invalidate([...suppliers, {
          supplier: '', options: [], chosen: '',
        }])}>Добавить поставщика</button>}
      <button type="button" className="button button-white" disabled={busy}
        onClick={() => void findSources()}>Найти проверенные прайсы</button>
      <button type="button" className="button button-primary"
        disabled={busy || !selectedSources()}
        onClick={() => void compare()}>Сравнить предложения</button>
    </div>
    {preview && <div role="group" aria-label="Проверенные предложения поставщиков">
      {preview.lines.map(line => {
        const selectedOffer = line.offers[selections[line.line_number] ?? -1];
        return <div key={line.line_number} className="tenant-quote-line">;
        <p><strong>Позиция {line.line_number}:</strong> {line.description}</p>
        {line.offers.length ? <label>Поставщик и товар для позиции {line.line_number}
          <select value={selections[line.line_number] ?? ''} disabled={busy}
            onChange={event => {
              const value = event.target.value;
              setSelections(current => {
                const next = { ...current };
                if (value === '') delete next[line.line_number];
                else next[line.line_number] = Number(value);
                return next;
              });
            }}>
            <option value="">Выберите предложение</option>
            {line.offers.map((offer, index) => <option key={offer.supplier_id + offer.catalog_version_id + offer.sku}
              value={index}>
              {offer.supplier_id} · {offer.sku} · {offer.product_name}
              {' · '}{money(offer.selling_total_rub)}
            </option>)}
          </select>
        </label> : <p role="status">Не удалось подобрать предложения с проверенными ценами. Выберите другой прайс или уточните заявку.</p>}
        {selectedOffer && <p>Выбранный источник: {selectedOffer.supplier_id} ·
          версия прайса {selectedOffer.catalog_version_id} ·
          SHA-256 {selectedOffer.source_sha256} ·
          закупка {money(selectedOffer.base_total_rub)} ·
          сумма КП {money(selectedOffer.selling_total_rub)}.</p>}
      </div>;
      })}
      <p>Цены сравниваются после проверки категории, характеристик, упаковки
        и условий расчёта. Наличие товара и срок поставки не подтверждены.</p>
      <button type="button" className="button button-primary" disabled={busy || !ready}
        onClick={() => void save()}>Создать КП с выбранными поставщиками</button>
    </div>}
  </section>;
}
