import { useEffect, useRef, useState } from 'react';
import { categories } from '../categories';
import type { CategorySpec } from '../categories';
import {
  commonVerifiedBatchGroup, MAX_BATCH_REVIEW, verifiedBatchGroup,
} from '../supplier-batch-review';
import { sessionApi } from '../tenant-session';
import { SupplierCheckpointProgress } from './SupplierCheckpointProgress';
import {
  createReviewedPublicationAttempt, publishConfirmedSupplierReview,
} from '../supplier-reviewed-publication';
import type { ReviewedPublicationAttempt } from '../supplier-reviewed-publication';
import type {
  SupplierCanonicalDecision, SupplierPrecisionMapping, SupplierPriceBasis,
  SupplierStagePreview, SupplierStageRow, ActiveTenantCatalog,
} from '../tenant-session';

type Props = {
  organizationId: string; supplierId: string; csrf: string;
  source: File; mapping: SupplierPrecisionMapping;
  stage: SupplierStagePreview; priceBasis: SupplierPriceBasis;
  quarantine: boolean; busy: boolean;
  onCanonicalReady: (file: File) => void;
  enableIsolatedCheckpointPublication?: boolean;
  onCheckpointPublished?: (catalog: ActiveTenantCatalog) => void;
};
type SaleUnit = 'штука' | 'пачка' | 'коробка';
type RoundingMode = 'half_up' | 'half_even' | 'down' | 'up';

// Technical compatibility, ink, printer and mixed-price goods still require
// individual decisions. A batch is a manual shortcut, never an ML assertion.
const BATCH_CATEGORIES = new Set([
  'paper', 'notebook', 'notepad', 'pencil', 'document_folder', 'binder',
  'plastic_sleeve', 'envelope', 'eraser', 'sharpener', 'staple', 'paperclip',
  'keyboard', 'mouse', 'mouse_pad', 'ruler',
]);

function readAttributes(
  category: CategorySpec, values: Record<string, string>, requireAll: boolean,
): { attributes: Record<string, string | number | string[]>; error: string | null } {
  const attributes: Record<string, string | number | string[]> = {};
  for (const field of category.fields) {
    const raw = (values[field.key] ?? '').trim();
    if (!raw) {
      if (requireAll) return { attributes, error: 'Укажите ' + field.label };
      continue;
    }
    if (field.type === 'number') {
      if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
        return { attributes, error: 'Проверьте числовую характеристику: ' + field.label };
      }
      attributes[field.key] = Number(raw);
    } else if (field.type === 'list') {
      attributes[field.key] = raw.split(',').map(value => value.trim()).filter(Boolean);
    } else attributes[field.key] = raw;
  }
  return { attributes, error: null };
}

export function SupplierCanonicalExport({
  organizationId, supplierId, csrf, source, mapping, stage, priceBasis,
  quarantine, busy, onCanonicalReady,
  enableIsolatedCheckpointPublication = false, onCheckpointPublished,
}: Props) {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const [reviews, setReviews] = useState<Record<number, SupplierCanonicalDecision>>({});
  const [batchLines, setBatchLines] = useState<number[]>([]);
  const [batchCategoryId, setBatchCategoryId] = useState('');
  const [batchValues, setBatchValues] = useState<Record<string, string>>({});
  const [batchPaperFormat, setBatchPaperFormat] = useState('');
  const [batchPaperGsm, setBatchPaperGsm] = useState('');
  const [batchVerified, setBatchVerified] = useState(false);
  const [batchError, setBatchError] = useState('');
  useEffect(() => {
    setBatchLines([]);
    setBatchCategoryId(''); setBatchValues({});
    setBatchPaperFormat(''); setBatchPaperGsm('');
    setBatchVerified(false);
    setBatchError('');
  }, [stage.offset]);
  const [selected, setSelected] = useState<SupplierStageRow | null>(null);
  const [exclude, setExclude] = useState(false);
  const [reason, setReason] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [unit, setUnit] = useState<SaleUnit | ''>('');
  const [pack, setPack] = useState('1');
  const [evidence, setEvidence] = useState('');
  const [brand, setBrand] = useState('');
  const [paperFormat, setPaperFormat] = useState('');
  const [paperGsm, setPaperGsm] = useState('');
  const [penColor, setPenColor] = useState('');
  const [penType, setPenType] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [rounding, setRounding] = useState<RoundingMode | ''>('');
  const [rounded, setRounded] = useState('');
  const [roundReason, setRoundReason] = useState('');
  const [checked, setChecked] = useState(false);
  const [priceBasisChecked, setPriceBasisChecked] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [publishedVersion, setPublishedVersion] = useState<string | null>(null);
  const attemptRef = useRef<{
    signature: string; attempt: ReviewedPublicationAttempt;
  } | null>(null);
  const category = categories.find(item => item.id === categoryId);
  const reviewedCount = Object.keys(reviews).length;
  const includedCount = Object.values(reviews).filter(item => item.action === 'include').length;
  const batchRows = stage.sampled_rows.filter(row =>
    batchLines.includes(row.line) && !reviews[row.line],
  );
  const batchGroup = commonVerifiedBatchGroup(batchRows);
  const batchCategory = categories.find(item => item.id === batchCategoryId);
  const batchChoice = (row: SupplierStageRow) => {
    if (!verifiedBatchGroup(row) || reviews[row.line] || sending || busy) return;
    setBatchLines(previous => previous.includes(row.line)
      ? previous.filter(line => line !== row.line)
      : previous.length < MAX_BATCH_REVIEW ? [...previous, row.line] : previous);
    setBatchCategoryId(''); setBatchValues({});
    setBatchPaperFormat(''); setBatchPaperGsm('');
    setBatchVerified(false);
    setBatchError('');
  };
  const confirmBatch = () => {
    if (!batchGroup || !batchCategory || !BATCH_CATEGORIES.has(batchCategory.id)
      || !batchVerified || busy || sending) return;
    const { attributes, error: attributeError } = readAttributes(
      batchCategory, batchValues, true,
    );
    if (attributeError) { setBatchError(attributeError); return; }
    const gsm = Number(batchPaperGsm);
    if (batchCategory.id === 'paper' && (
      !batchPaperFormat.trim() || !Number.isInteger(gsm) || gsm < 1 || gsm > 1000
    )) {
      setBatchError('Укажите проверенные формат и плотность бумаги для каждой строки.');
      return;
    }
    const applied: Record<number, SupplierCanonicalDecision> = {};
    for (const row of batchRows) {
      applied[row.line] = {
        action: 'include', line: row.line,
        source_row_sha256: row.source_row_sha256, confirmed: true,
        category: batchCategory.id, sale_unit: batchGroup.saleUnit,
        pieces_per_sale_unit: batchGroup.piecesPerSaleUnit,
        attributes, brand: null,
        paper_format: batchCategory.id === 'paper' ? batchPaperFormat.trim() : null,
        paper_gsm: batchCategory.id === 'paper' ? gsm : null,
        pen_ink_color: null, pen_ink_type: null,
        price_unit_review_evidence: null, precision_review: null,
      };
    }
    setReviews(previous => ({ ...previous, ...applied }));
    setPublishedVersion(null);
    setBatchLines([]);
    setBatchCategoryId(''); setBatchValues({});
    setBatchPaperFormat(''); setBatchPaperGsm('');
    setBatchVerified(false);
    setBatchError('');
  };

  const open = (row: SupplierStageRow) => {
    setSelected(row);
    setExclude(false); setReason('');
    setCategoryId('');
    setUnit(''); setPack('1'); setEvidence('');
    setBrand(''); setPaperFormat(''); setPaperGsm('');
    setPenColor(''); setPenType(''); setValues({});
    setRounding(''); setRounded(''); setRoundReason('');
    setChecked(false); setError('');
  };

  const save = () => {
    if (!selected || !checked) return;
    const shared = {
      line: selected.line,
      source_row_sha256: selected.source_row_sha256,
      confirmed: true as const,
    };
    let decision: SupplierCanonicalDecision;
    if (exclude) {
      if (reason.trim().length < 8 || reason.trim().length > 500) {
        setError('Укажите причину исключения от 8 до 500 символов.');
        return;
      }
      decision = { action: 'exclude', ...shared, reason: reason.trim() };
    } else {
      const pieces = Number(pack);
      if (!category || !unit || !Number.isInteger(pieces) || pieces < 1
        || pieces > 100000 || (unit === 'штука' && pieces !== 1)) {
        setError('Проверьте категорию, единицу продажи и размер упаковки.');
        return;
      }
      const { attributes, error: attributeError } = readAttributes(category, values, false);
      if (attributeError) { setError(attributeError); return; }
      const needsPrecision = selected.issues.includes('price_precision_requires_review');
      if (needsPrecision && (!rounding || !/^(?:0|[1-9][0-9]{0,11})\.[0-9]{2}$/.test(rounded)
        || roundReason.trim().length < 32)) {
        setError('Точная цена требует ручного округления и обоснования от 32 символов.');
        return;
      }
      decision = {
        action: 'include', ...shared, category: category.id,
        sale_unit: unit, pieces_per_sale_unit: pieces,
        attributes, brand: brand.trim() || null,
        paper_format: category.id === 'paper' ? paperFormat.trim() || null : null,
        paper_gsm: category.id === 'paper' && paperGsm ? Number(paperGsm) : null,
        pen_ink_color: category.id === 'pen' ? penColor.trim() || null : null,
        pen_ink_type: category.id === 'pen' ? penType.trim() || null : null,
        price_unit_review_evidence: evidence.trim() || null,
        precision_review: needsPrecision && rounding
          ? { price_rub: rounded, rounding_mode: rounding, explanation: roundReason.trim() }
          : null,
      };
    }
    setReviews(previous => ({ ...previous, [selected.line]: decision }));
    setPublishedVersion(null);
    setBatchLines([]);
    setBatchCategoryId(''); setBatchValues({});
    setBatchPaperFormat(''); setBatchPaperGsm('');
    setBatchVerified(false);
    setSelected(null); setChecked(false); setError('');
  };

  const prepare = async () => {
    if (sending || busy || reviewedCount !== stage.total_rows || !includedCount
      || !priceBasisChecked) return;
    setSending(true); setError('');
    try {
      const file = await sessionApi.exportReviewedSupplier(
        organizationId, supplierId, source, csrf, {
          source_sha256: stage.source_sha256, mapping, price_basis: priceBasis,
          explicitly_confirmed_price_basis: true,
          formula_policy: quarantine ? 'quarantine_rows' : 'reject',
          decisions: Object.values(reviews).sort((a, b) => a.line - b.line),
        },
      );
      if (mounted.current) onCanonicalReady(file);
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Проверка каталога не удалась.');
      }
    } finally {
      if (mounted.current) setSending(false);
    }
  };

  const publishCheckpoint = async () => {
    if (!enableIsolatedCheckpointPublication || sending || busy
      || stage.next_offset !== null || stage.total_rows < 1 || stage.total_rows > 50
      || reviewedCount !== stage.total_rows || !includedCount || !priceBasisChecked) return;
    const review = {
      source_sha256: stage.source_sha256, mapping, price_basis: priceBasis,
      explicitly_confirmed_price_basis: true as const,
      formula_policy: quarantine ? 'quarantine_rows' as const : 'reject' as const,
      decisions: Object.values(reviews).sort((a, b) => a.line - b.line),
    };
    // Retain both keys for an ambiguous network result; a changed review or
    // source always generates a fresh pair, never reuses stale idempotency.
    const signature = JSON.stringify({ organizationId, supplierId, source: source.name, review });
    setSending(true); setError(''); setPublishedVersion(null);
    try {
      const previous = attemptRef.current;
      const attempt = previous?.signature === signature
        ? previous.attempt : createReviewedPublicationAttempt();
      attemptRef.current = { signature, attempt };
      const result = await publishConfirmedSupplierReview({
        organizationId, supplierId, source, csrf, review,
        totalRows: stage.total_rows, attempt,
      });
      if (mounted.current) {
        setPublishedVersion(result.version_id);
        onCheckpointPublished?.(result);
      }
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Публикация не выполнена.');
      }
    } finally {
      if (mounted.current) setSending(false);
    }
  };

  return <div className="supplier-decision" role="group" aria-label="Преобразование проверенных строк">
    <h3>Подготовка прайса к загрузке</h3>
    <p className="access-hint">Проверено {reviewedCount} из {stage.total_rows}.
      В каталог включено {includedCount}. Решение нужно для каждой строки, включая исключённые.
      До проверки всех строк публикация невозможна.</p>
    {error && <p role="alert" className="alert error">{error}</p>}
    <p className="access-hint">Можно одновременно проверить не более {MAX_BATCH_REVIEW} выбранных
      строк с одинаковыми категорией, единицей продажи и упаковкой.
      Строки с замечаниями и ранее подтверждённые строки проверяйте по отдельности.</p>
    <div className="supplier-cases">
      {stage.sampled_rows.map(row => <div key={row.line}>
        <button type="button" className="button button-white" disabled={busy || sending}
          onClick={() => open(row)}>
          Строка {row.line}: {row.name || 'Без названия'} ·
          {' '}{row.supplier_sku || 'Без артикула'} ·
          {' '}{row.price_rub ? row.price_rub + ' RUB' : 'Цена не подтверждена'} — {
            reviews[row.line] ? 'решение сохранено' : 'проверить'
          }
        </button>
        {verifiedBatchGroup(row) && !reviews[row.line] &&
          <label className="tenant-checkbox">
            <input type="checkbox" checked={batchLines.includes(row.line)}
              disabled={busy || sending || (!batchLines.includes(row.line)
                && batchLines.length >= MAX_BATCH_REVIEW)}
              onChange={() => batchChoice(row)}/>
            Проверил строку {row.line} для группового подтверждения
          </label>}
      </div>)}
    </div>
    <div className="supplier-profile-fields" role="group" aria-label="Проверка группы товаров">
      <h3>Проверенные строки текущей страницы: {batchRows.length}</h3>
      {batchRows.length > 0 && !batchGroup && <p className="supplier-risk">
        Выбранные товары имеют разные исходные категории, фасовку или единицу продажи.
        Выберите строки с одинаковыми параметрами и подтвердите их отдельно.
      </p>}
      {batchGroup && <>
        <p>Исходная категория: {batchGroup.sourceCategory}. Единица: {batchGroup.saleUnit}.
          Штук в единице: {batchGroup.piecesPerSaleUnit}.</p>
        <label>Подтверждённая категория всех выбранных товаров
          <select value={batchCategoryId} disabled={busy || sending}
            onChange={e => {
              setBatchCategoryId(e.target.value);
              setBatchValues({}); setBatchVerified(false); setBatchError('');
            }}>
            <option value="">Выберите после проверки каждого названия</option>
            {categories.filter(item => BATCH_CATEGORIES.has(item.id)).map(item =>
              <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </label>
        {batchCategory?.fields.map(field => <label key={field.key}>
          Общая характеристика: {field.label}
          <input value={batchValues[field.key] ?? ''} disabled={busy || sending}
            placeholder={field.placeholder ?? (field.type === 'list' ? 'Через запятую' : undefined)}
            onChange={e => { setBatchValues(previous => ({
              ...previous, [field.key]: e.target.value,
            })); setBatchVerified(false); }}/>
        </label>)}
        {batchCategoryId === 'paper' && <>
          <label>Общий проверенный формат бумаги
            <input value={batchPaperFormat} disabled={busy || sending}
              onChange={e => { setBatchPaperFormat(e.target.value); setBatchVerified(false); }}/>
          </label>
          <label>Общая проверенная плотность, г/м²
            <input type="number" min={1} max={1000} value={batchPaperGsm}
              disabled={busy || sending}
              onChange={e => { setBatchPaperGsm(e.target.value); setBatchVerified(false); }}/>
          </label>
        </>}
        <label className="tenant-checkbox"><input type="checkbox"
          checked={batchVerified} disabled={busy || sending || !batchCategoryId}
          onChange={e => setBatchVerified(e.target.checked)}/>
          Я сверил каждую отмеченную строку оригинала: товар, категорию,
          параметры, единицу цены и упаковку. Все общие параметры одинаковы.
        </label>
        <button type="button" className="button button-white"
          disabled={busy || sending || !batchVerified || !batchCategoryId}
          onClick={confirmBatch}>
          Применить проверенные параметры к {batchRows.length} строкам
        </button>
      </>}
      {batchError && <p role="alert" className="alert error">{batchError}</p>}
    </div>
    {selected && <div className="access-form" key={selected.source_row_sha256}>
      <h3>Строка {selected.line} — {selected.name || 'Без названия'}</h3>
      <p>Артикул: {selected.supplier_sku || 'отсутствует'}.
        Цена: {selected.price_rub ?? 'не подтверждена'}.
        {selected.source_high_precision_price
          ? ' Исходная точная цена: ' + selected.source_high_precision_price + '.' : ''}
        Замечания: {selected.issues.join(', ') || 'нет'}.</p>
      <label className="tenant-checkbox"><input type="checkbox" checked={exclude}
        onChange={e => setExclude(e.target.checked)}/>
        Исключить эту строку из каталога</label>
      {exclude ? <label>Причина исключения
        <textarea value={reason} maxLength={500} onChange={e => setReason(e.target.value)}/>
      </label> : <>
        <label>Категория товара
          <select value={categoryId} onChange={e => {
            setCategoryId(e.target.value); setValues({});
          }}>
            <option value="">Выберите после проверки</option>
            {categories.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label>Единица продажи
          <select value={unit} onChange={e => setUnit(e.target.value as SaleUnit | '')}>
            <option value="">Выберите по исходнику</option>
            <option value="штука">Штука</option><option value="пачка">Пачка</option>
            <option value="коробка">Коробка</option>
          </select>
        </label>
        <label>Количество штук в единице продажи
          <input type="number" min={1} max={100000} value={pack}
            onChange={e => setPack(e.target.value)}/>
        </label>
        <label>Бренд (если указан в источнике)
          <input value={brand} maxLength={500} onChange={e => setBrand(e.target.value)}/>
        </label>
        {categoryId === 'paper' && <>
          <label>Формат бумаги<input value={paperFormat}
            onChange={e => setPaperFormat(e.target.value)}/></label>
          <label>Плотность бумаги, г/м²<input type="number" min={1}
            value={paperGsm} onChange={e => setPaperGsm(e.target.value)}/></label>
        </>}
        {categoryId === 'pen' && <>
          <label>Цвет чернил<input value={penColor}
            onChange={e => setPenColor(e.target.value)}/></label>
          <label>Тип чернил<input value={penType}
            onChange={e => setPenType(e.target.value)}/></label>
        </>}
        {category?.fields.map(field => <label key={field.key}>{field.label}
          <input value={values[field.key] ?? ''} onChange={e => setValues(previous => ({
            ...previous, [field.key]: e.target.value,
          }))} placeholder={field.placeholder ?? (field.type === 'list' ? 'Значения через запятую' : undefined)}/>
        </label>)}
        <label>Подтверждение единицы цены (при необходимости, от 32 символов)
          <textarea rows={2} maxLength={500} value={evidence}
            onChange={e => setEvidence(e.target.value)}/>
        </label>
        {selected.issues.includes('price_precision_requires_review') && <>
          <label>Режим округления
            <select value={rounding} onChange={e => setRounding(e.target.value as RoundingMode | '')}>
              <option value="">Выберите</option>
              <option value="half_up">Половины вверх</option>
              <option value="half_even">Половины к чётному</option>
              <option value="down">Вниз</option>
              <option value="up">Вверх</option>
            </select>
          </label>
          <label>Проверенная цена, ₽ (два знака после точки)
            <input value={rounded} onChange={e => setRounded(e.target.value)}/>
          </label>
          <label>Обоснование округления
            <textarea value={roundReason} maxLength={500}
              onChange={e => setRoundReason(e.target.value)}/>
          </label>
        </>}
      </>}
      <label className="tenant-checkbox"><input type="checkbox" checked={checked}
        onChange={e => setChecked(e.target.checked)}/>
        Я лично проверил эту строку оригинала и подтверждаю решение</label>
      <button type="button" className="button button-white"
        disabled={!checked || busy || sending} onClick={save}>Сохранить решение по строке</button>
    </div>}
    <label className="tenant-checkbox"><input type="checkbox" checked={priceBasisChecked}
      onChange={e => setPriceBasisChecked(e.target.checked)}/>
      Я проверил тип цены: {priceBasis}, применённый ко всему исходному файлу</label>
    <button type="button" className="button button-primary"
      disabled={busy || sending || reviewedCount !== stage.total_rows
        || !includedCount || !priceBasisChecked}
      onClick={() => void prepare()}>
      {sending ? 'Формирование…' : 'Подготовить CSV для загрузки'}
    </button>
    <p className="access-hint">Готовый CSV нужно дополнительно проверить в разделе загрузки прайса.
      Подготовка CSV не добавляет товары в действующий прайс.</p>
    {enableIsolatedCheckpointPublication && stage.total_rows > 50 && <SupplierCheckpointProgress
      key={JSON.stringify({
        organizationId, supplierId, source: stage.source_sha256,
        mapping, priceBasis, quarantine,
      })}
      organizationId={organizationId} supplierId={supplierId}
      source={source} csrf={csrf}
      review={{
        source_sha256: stage.source_sha256, mapping,
        price_basis: priceBasis,
        formula_policy: quarantine ? 'quarantine_rows' : 'reject',
        explicitly_confirmed_price_basis: true,
        decisions: [],
      }}
      stage={stage} reviews={reviews} busy={busy || sending}
      priceBasisConfirmed={priceBasisChecked}
      onPublished={onCheckpointPublished}
      onRestored={decisions => {
        setReviews(decisions);
        setSelected(null);
        setBatchLines([]);
        setPublishedVersion(null);
        setError('');
        setPriceBasisChecked(false);
      }}
    />}
    {enableIsolatedCheckpointPublication && stage.total_rows <= 50 && <>
      <p className="access-hint">Только изолированный стенд: подтверждение решений
        и публикация всех сохранённых строк одной версией. В рабочем сервисе отключено.</p>
      <button type="button" className="button button-primary"
        disabled={busy || sending || stage.next_offset !== null
          || stage.total_rows > 50 || stage.total_rows < 1
          || reviewedCount !== stage.total_rows || !includedCount || !priceBasisChecked}
        onClick={() => void publishCheckpoint()}>
        {sending ? 'Публикация…' : 'Подтвердить и опубликовать (тестовый стенд)'}
      </button>
      {publishedVersion && <p role="status">
        Опубликована проверенная версия каталога: {publishedVersion}
      </p>}
    </>}
  </div>;
}
