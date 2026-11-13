import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ClipboardCheck, FileSearch, ShieldCheck } from 'lucide-react';
import { sessionApi } from '../tenant-session';
import { SupplierStageRows } from './SupplierStageRows';
import { readableSupplierIssue } from '../supplier-issues';
import { SupplierUnitReviewForm } from './SupplierUnitReviewForm';
import type {
  SupplierPrecisionCase, SupplierPrecisionDecision, SupplierPrecisionFinding,
  SupplierSourceInspection, SupplierMappingProfile,
  SupplierMappingHistory, SupplierPriceBasis,
  SupplierPrecisionHistory, SupplierPrecisionMapping, SupplierPrecisionPreview,
  SupplierRoundingMode, SupplierStagePreview,
} from '../tenant-session';

const SupplierCanonicalExport = lazy(async () => ({
  default: (await import('./SupplierCanonicalExport')).SupplierCanonicalExport,
}));

type Props = {
  organizationId: string; csrf: string | null;
  onCanonicalReady?: (file: File) => void;
};
type Columns = {
  header_row: number; name: number; price: number;
  sku: number; currency: number; category: number;
  sale_unit: number; pieces_per_sale_unit: number;
};
const INITIAL_COLUMNS: Columns = {
  header_row: 1, name: 2, price: 3,
  sku: 1, currency: 4, category: 0, sale_unit: 5, pieces_per_sale_unit: 6,
};
const REVIEW_LABELS: Record<SupplierPrecisionFinding, string> = {
  approve_rounding: 'Подтвердить округление',
  needs_supplier_confirmation: 'Требуется подтверждение поставщика',
  reject_source_price: 'Отклонить цену из файла',
};
const shaPattern = /^[0-9a-f]{64}$/;
const supplierPattern = /^[A-Za-z0-9_.-]{1,120}$/;
const centsPattern = /^(?:0|[1-9][0-9]{0,11})\.[0-9]{2}$/;

function makeMapping(
  columns: Columns, sheet: string, delimiter: ';' | ',' | '\t',
  encoding: 'utf-8-sig' | 'utf-8' | 'cp1251', confirmedRub: boolean,
): SupplierPrecisionMapping {
  if (columns.name < 1 || columns.name > 64
    || columns.price < 1 || columns.price > 64
    || columns.header_row < 1 || columns.header_row > 20
    || !Object.values(columns).every(Number.isInteger)
    || [columns.sku, columns.currency, columns.category,
      columns.sale_unit, columns.pieces_per_sale_unit]
      .some(value => value < 0 || value > 64)) {
    throw new Error('Проверьте номера столбцов и строки заголовка.');
  }
  const optional = (number: number) => number === 0 ? null : number - 1;
  return {
    header_row: columns.header_row,
    name: columns.name - 1, price: columns.price - 1,
    sku: optional(columns.sku), currency: optional(columns.currency),
    category: optional(columns.category),
    sale_unit: optional(columns.sale_unit),
    pieces_per_sale_unit: optional(columns.pieces_per_sale_unit),
    ...(columns.currency === 0 && confirmedRub ? { confirmed_currency: 'RUB' as const } : {}),
    ...(sheet.trim() ? { sheet: sheet.trim() } : {}),
    delimiter, encoding,
  };
}

export function SupplierPrecisionReview({
  organizationId, csrf, onCanonicalReady,
}: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [supplierId, setSupplierId] = useState('');
  const [columns, setColumns] = useState<Columns>(INITIAL_COLUMNS);
  const [sheet, setSheet] = useState('');
  const [delimiter, setDelimiter] = useState<';' | ',' | '\t'>(';');
  const [encoding, setEncoding] = useState<'utf-8-sig' | 'utf-8' | 'cp1251'>('utf-8-sig');
  const [confirmedRub, setConfirmedRub] = useState(false);
  const [quarantine, setQuarantine] = useState(false);
  const [inspection, setInspection] = useState<SupplierSourceInspection | null>(null);
  const [mappingConfirmed, setMappingConfirmed] = useState(false);
  const [storedMapping, setStoredMapping] = useState<SupplierMappingProfile | null>(null);
  const [profileHistory, setProfileHistory] = useState<SupplierMappingHistory | null>(null);
  const [compareA, setCompareA] = useState('');
  const [compareB, setCompareB] = useState('');
  const [profileEvidence, setProfileEvidence] = useState('');
  const [priceBasis, setPriceBasis] = useState<SupplierPriceBasis>('standard');
  const [preview, setPreview] = useState<SupplierPrecisionPreview | null>(null);
  const [stagePreview, setStagePreview] = useState<SupplierStagePreview | null>(null);
  const [stageMapping, setStageMapping] = useState<SupplierPrecisionMapping | null>(null);
  const [selectedStageLine, setSelectedStageLine] = useState<number | null>(null);
  const stageGeneration = useRef(0);
  const [reviewedMapping, setReviewedMapping] = useState<SupplierPrecisionMapping | null>(null);
  const [selectedLine, setSelectedLine] = useState<number | null>(null);
  const [history, setHistory] = useState<SupplierPrecisionHistory | null | 'unavailable'>(null);
  const [finding, setFinding] = useState<SupplierPrecisionFinding>('needs_supplier_confirmation');
  const [roundingMode, setRoundingMode] = useState<SupplierRoundingMode | ''>('');
  const [roundedPrice, setRoundedPrice] = useState('');
  const [rationale, setRationale] = useState('');
  const [personChecked, setPersonChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const chosen = preview?.rows.find(item => item.line === selectedLine) ?? null;
  const chosenStage = stagePreview?.sampled_rows.find(
    item => item.line === selectedStageLine,
  ) ?? null;
  const canApprove = !!chosen && chosen.currency_confirmed_rub
    && !chosen.formula_provenance_unverified;

  const changeSupplier = (next: string) => {
    invalidate();
    setSupplierId(next);
  };

  const resetDecision = () => {
    setFinding('needs_supplier_confirmation');
    setRoundingMode('');
    setRoundedPrice('');
    setRationale('');
    setPersonChecked(false);
  };
  const resetUnit = () => setSelectedStageLine(null);
  const invalidateReviewOnly = () => {
    stageGeneration.current += 1;
    resetUnit();
    setStagePreview(null);
    setStageMapping(null);
    setPreview(null);
    setReviewedMapping(null);
    setSelectedLine(null);
    setHistory(null);
    setError('');
    setNotice('');
    resetDecision();
  };
  const invalidate = (dropInspection = true) => {
    if (dropInspection) {
      setInspection(null);
      setStoredMapping(null);
      setProfileHistory(null);
      setCompareA('');
      setCompareB('');
      setPriceBasis('standard');
    }
    setProfileEvidence('');
    setMappingConfirmed(false);
    invalidateReviewOnly();
  };

  const refreshHistory = async (row: SupplierPrecisionCase, digest: string) => {
    const saved = await sessionApi.supplierPrecisionHistory(
      organizationId, supplierId, digest, row.line,
    );
    setHistory(saved);
  };

  useEffect(() => {
    if (!preview || !chosen) return;
    let cancelled = false;
    void sessionApi.supplierPrecisionHistory(
      organizationId, supplierId, preview.source_sha256, chosen.line,
    ).then(data => { if (!cancelled) setHistory(data); })
      .catch(cause => {
        if (!cancelled) {
          setHistory('unavailable');
          setError(cause instanceof Error ? cause.message : 'Не удалось загрузить историю проверок.');
        }
      });
    return () => { cancelled = true; };
  }, [organizationId, supplierId, preview, chosen]);

  const showPage = async (
    start: number, mapping: SupplierPrecisionMapping, source: File,
  ) => {
    if (!csrf || !supplierPattern.test(supplierId) || !mappingConfirmed || !inspection) {
      setError('Укажите идентификатор поставщика и войдите повторно.');
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await sessionApi.previewSupplierPrecision(
        organizationId, supplierId, source,
        { ...mapping, source_sha256: inspection.source_sha256 },
        csrf, start, 50, quarantine,
      );
      if (!shaPattern.test(result.source_sha256) || result.published
        || result.model_training_authorized) {
        throw new Error('Не удалось подтвердить результат проверки прайса. Повторите проверку.');
      }
      setPreview(result);
      setReviewedMapping(mapping);
      setSelectedLine(null);
      setHistory(null);
      resetDecision();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось проверить прайс.');
    } finally {
      setBusy(false);
    }
  };

  const showStagePage = async (
    start: number, mapping: SupplierPrecisionMapping, source: File,
  ) => {
    if (!csrf || !supplierPattern.test(supplierId) || !mappingConfirmed || !inspection) {
      setError('Для просмотра строк подтвердите столбцы и войдите в аккаунт.');
      return;
    }
    const generation = ++stageGeneration.current;
    setBusy(true);
    setError('');
    try {
      const result = await sessionApi.previewSupplierStage(
        organizationId, supplierId, source,
        { ...mapping, source_sha256: inspection.source_sha256 },
        csrf, start, 50, quarantine,
      );
      if (generation !== stageGeneration.current) return;
      if (result.source_sha256 !== inspection.source_sha256
        || result.offset !== start || result.canonical_importable
        || result.model_training_authorized || !result.requires_confirmation) {
        throw new Error('Просмотр строк не соответствует проверенному исходнику.');
      }
      setStagePreview(result);
      setStageMapping(mapping);
      resetUnit();
    } catch (cause) {
      if (generation === stageGeneration.current) {
        setStagePreview(null);
        setError(cause instanceof Error ? cause.message : 'Не удалось прочитать строки.');
      }
    } finally {
      if (generation === stageGeneration.current) setBusy(false);
    }
  };

  const inspectAllRows = () => {
    if (!file || !inspection || !mappingConfirmed) return;
    try {
      const mapping = makeMapping(columns, sheet, delimiter, encoding, confirmedRub);
      void showStagePage(0, mapping, file);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Проверьте столбцы прайса.');
    }
  };

  const detectStructure = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file || !csrf || !supplierPattern.test(supplierId)) return;
    if (!/\.(csv|xls|xlsx)$/i.test(file.name) || file.size > 10 * 1024 * 1024) {
      setError('Выберите CSV, XLS или XLSX размером до 10 МБ.');
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    invalidate();
    try {
      const result = await sessionApi.inspectSupplierStructure(
        organizationId, supplierId, file, csrf, sheet.trim() || undefined,
        quarantine, encoding === 'cp1251' ? 'cp1251' : undefined,
      );
      if (!shaPattern.test(result.source_sha256)
        || !result.requires_confirmation || result.canonical_importable
        || result.model_training_authorized
        || !['csv', 'xls', 'xlsx'].includes(result.source_format)) {
        throw new Error('Не удалось подтвердить структуру файла. Проверьте прайс и повторите анализ.');
      }
      setInspection(result);
      if (result.issues.includes('sheet_selection_required')) {
        setNotice('Выберите лист Excel и повторите определение столбцов.');
      } else {
        setNotice('Предложение подготовлено. Сверьте столбцы с оригиналом.');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось определить столбцы.');
    } finally {
      setBusy(false);
    }
  };

  const findSaved = async () => {
    if (!file || !csrf || !inspection) return;
    setBusy(true);
    setError('');
    try {
      const stored = await sessionApi.matchSupplierMapping(
        organizationId, supplierId, file, csrf,
        inspection.selected_sheet ?? undefined,
      );
      if (stored.source_sha256 !== inspection.source_sha256
        || stored.canonical_importable || stored.model_training_authorized) {
        throw new Error('Полученные настройки не соответствуют исходному файлу.');
      }
      setStoredMapping(stored);
      setNotice(stored.found
        ? 'Найдены сохранённые настройки столбцов. Сверьте их с заголовками в файле.'
        : 'Сохранённых настроек для этих столбцов пока нет.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось найти настройки столбцов.');
    } finally {
      setBusy(false);
    }
  };

  const loadProfileHistory = async (offset = 0) => {
    if (!supplierPattern.test(supplierId)) return;
    setBusy(true);
    setError('');
    try {
      const result = await sessionApi.supplierMappingHistory(
        organizationId, supplierId, offset,
      );
      if (result.canonical_importable || result.model_training_authorized) {
        throw new Error('Сохранённые настройки столбцов не подтверждают товары автоматически.');
      }
      setProfileHistory(previous => offset === 0 || !previous ? result : {
        ...result, items: [...previous.items, ...result.items],
      });
      if (offset === 0) {
        setCompareA(result.items[0]?.profile_sha256 ?? '');
        setCompareB(result.items[1]?.profile_sha256 ?? '');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось загрузить историю настроек столбцов.');
    } finally {
      setBusy(false);
    }
  };

  const profileA = profileHistory?.items.find(item => item.profile_sha256 === compareA);
  const profileB = profileHistory?.items.find(item => item.profile_sha256 === compareB);
  const comparisonFields = [
    'fingerprint', 'source_format', 'columns', 'header_row', 'sheet',
    'delimiter', 'encoding', 'items_key', 'confirmed_currency',
    'price_basis', 'approved_source_sha256',
  ] as const;
  const compareValue = (value: unknown) => value == null
    ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);

  const useSaved = () => {
    const columns = storedMapping?.columns;
    if (!storedMapping?.found || !columns || !inspection) return;
    invalidate(false);
    const oneBased = (key: string) => typeof columns[key] === 'number'
      ? columns[key] + 1 : 0;
    setColumns({
      sku: oneBased('sku'), name: oneBased('name'), price: oneBased('price'),
      currency: oneBased('currency'), category: oneBased('category'),
      sale_unit: oneBased('sale_unit'),
      pieces_per_sale_unit: oneBased('pieces_per_sale_unit'),
      header_row: storedMapping.header_row ?? 1,
    });
    setPriceBasis((storedMapping.price_basis || 'standard') as SupplierPriceBasis);
    setSheet(storedMapping.sheet ?? '');
    if (inspection.delimiter === ';' || inspection.delimiter === ','
      || inspection.delimiter === '\t') setDelimiter(inspection.delimiter);
    if (inspection.encoding === 'utf-8' || inspection.encoding === 'utf-8-sig'
      || inspection.encoding === 'cp1251') setEncoding(inspection.encoding);
    setConfirmedRub(false);
    setNotice('Настройки столбцов применены. Проверьте каждое соответствие и подтвердите.');
  };

  const saveMapping = async () => {
    if (!file || !inspection || !csrf || !mappingConfirmed
      || profileEvidence.trim().length < 8 || profileEvidence.trim().length > 500) return;
    setBusy(true);
    setError('');
    try {
      const mapping = makeMapping(columns, sheet, delimiter, encoding, confirmedRub);
      const saved = await sessionApi.saveSupplierMapping(
        organizationId, supplierId, file, csrf, inspection, mapping,
        priceBasis, profileEvidence.trim(),
      );
      if (!saved.found || !saved.reviewed
        || saved.source_sha256 !== inspection.source_sha256) {
        throw new Error('Не удалось подтвердить сохранение настроек столбцов. Обновите историю.');
      }
      setStoredMapping(saved);
      setNotice('Соответствие столбцов сохранено. Товары и цены пока не загружены в прайс.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось сохранить настройки столбцов.');
    } finally {
      setBusy(false);
    }
  };

  const applyProposal = () => {
    const mapping = inspection?.stage_mapping;
    if (!mapping || typeof mapping.name !== 'number'
      || typeof mapping.price !== 'number'
      || typeof mapping.header_row !== 'number') return;
    invalidate(false);
    const oneBased = (value: number | null | undefined) => (
      typeof value === 'number' ? value + 1 : 0
    );
    setColumns({
      header_row: mapping.header_row,
      name: mapping.name + 1,
      price: mapping.price + 1,
      sku: oneBased(mapping.sku),
      currency: oneBased(mapping.currency),
      category: oneBased(mapping.category),
      sale_unit: oneBased(mapping.sale_unit),
      pieces_per_sale_unit: oneBased(mapping.pieces_per_sale_unit),
    });
    setSheet(mapping.sheet ?? '');
    if (mapping.delimiter === ';' || mapping.delimiter === ','
      || mapping.delimiter === '\t') setDelimiter(mapping.delimiter);
    if (mapping.encoding === 'utf-8' || mapping.encoding === 'utf-8-sig'
      || mapping.encoding === 'cp1251') setEncoding(mapping.encoding);
    setConfirmedRub(false);
    setNotice('Предложенные настройки перенесены в поля. Проверьте столбцы и подтвердите.');
  };

  const inspect = () => {
    if (!file || !inspection || !mappingConfirmed) return;
    if (!/\.(csv|xls|xlsx)$/i.test(file.name) || file.size > 10 * 1024 * 1024) {
      setError('Выберите CSV, XLS или XLSX размером до 10 МБ.');
      return;
    }
    try {
      const mapping = makeMapping(columns, sheet, delimiter, encoding, confirmedRub);
      void showPage(0, mapping, file);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Проверьте столбцы прайса.');
    }
  };

  const record = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file || !csrf || !preview || !reviewedMapping || !chosen || !personChecked
      || rationale.trim().length < 32 || rationale.trim().length > 500
      || (finding === 'approve_rounding' && (!canApprove
        || !roundingMode || !centsPattern.test(roundedPrice)))) {
      setError('Выберите решение, проверьте исходный файл, добавьте обоснование и подтвердите проверку.');
      return;
    }
    const decision: SupplierPrecisionDecision = {
      line: chosen.line, source_row_sha256: chosen.source_row_sha256,
      finding,
      rounding_mode: finding === 'approve_rounding' ? roundingMode as SupplierRoundingMode : null,
      rounded_price_rub: finding === 'approve_rounding' ? roundedPrice : null,
      rationale: rationale.trim(),
    };
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const recorded = await sessionApi.attestSupplierPrecision(
        organizationId, supplierId, file, reviewedMapping, preview,
        decision, csrf, quarantine,
      );
      if (!recorded.recorded || recorded.published
        || recorded.source_row_sha256 !== chosen.source_row_sha256) {
        throw new Error('Не удалось подтвердить сохранение заключения. Проверьте историю перед повторной попыткой.');
      }
      resetDecision();
      setNotice('Заключение сохранено. Цена пока не добавлена в действующий прайс.');
      try {
        await refreshHistory(chosen, preview.source_sha256);
      } catch {
        setHistory('unavailable');
        setError('Заключение сохранено, но история временно недоступна.');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Сохранить решение не удалось.');
    } finally {
      setBusy(false);
    }
  };

  const selectRow = (row: SupplierPrecisionCase) => {
    setSelectedLine(row.line);
    setHistory(null);
    setError('');
    setNotice('');
    resetDecision();
  };

  const changeColumn = (field: keyof Columns, value: string) => {
    invalidate(false);
    setColumns(previous => ({ ...previous, [field]: Number(value) }));
  };

  return <section className="access-catalog-state supplier-precision" aria-labelledby="precision-title">
    <h2 id="precision-title"><ClipboardCheck size={20} aria-hidden="true"/> Проверка цен поставщиков</h2>
    <p className="access-hint">Выберите прайс и проверьте колонки, цены и упаковки.</p>
    <details><summary>Что подтверждает проверка</summary>
      <p className="access-hint">Сохранённое заключение не публикует товары,
        не округляет суммы автоматически и не разрешает обучение модели.</p>
    </details>
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <div className="alert success" role="status">{notice}</div>}
    <form className="access-form" onSubmit={event => void detectStructure(event)}>
      <label>Идентификатор поставщика
        <input type="text" required pattern="[A-Za-z0-9_.-]{1,120}"
          maxLength={120} disabled={busy} value={supplierId}
          placeholder="supplier-001" onChange={event => changeSupplier(event.target.value)}/>
      </label>
      <label>Оригинал прайс-листа
        <input type="file" accept=".csv,.xls,.xlsx" disabled={busy}
          onChange={event => {
            invalidate(); setFile(event.target.files?.[0] ?? null);
          }}/>
      </label>
      {inspection && <div className="supplier-inspection" role="group" aria-label="Предложенное соответствие столбцов">
        <p><strong>Обнаружено строк: {inspection.row_count}</strong>. Формат: {
          inspection.source_format.toUpperCase()
        }. Требуется ручное подтверждение.</p>
        {inspection.available_sheets.length > 1 &&
          <label>Выберите лист из файла
            <select value={sheet} onChange={event => {
              invalidate(); setSheet(event.target.value);
            }}>
              <option value="">Выберите лист для повторного анализа</option>
              {inspection.available_sheets.map(option =>
                <option key={option} value={option}>{option}</option>)}
            </select>
          </label>}
        {inspection.issues.length > 0 && <>
          <ul className="supplier-inspection-issues">
            {inspection.issues.filter(issue =>
              !(issue === 'manual_name_selection_required' && inspection.issues.includes('ambiguous_name_columns'))
              && !(issue === 'manual_price_selection_required' && inspection.issues.includes('ambiguous_price_columns')))
              .map(issue => <li key={issue}>{readableSupplierIssue(issue)}</li>)}
          </ul>
          <details><summary>Технические сведения о замечаниях</summary>
            <p className="access-hint">SHA-256 оригинала: <code>{inspection.source_sha256}</code></p>
            <small className="access-hint">{inspection.issues.join(', ')}</small>
          </details>
        </>}
        {inspection.observed_columns.length > 0 && <>
          <div className="supplier-inspection-table">
            <table>
              <thead><tr>{inspection.observed_columns.map((name, index) =>
                <th scope="col" key={index}>{name || 'Столбец ' + (index + 1)}</th>)}</tr></thead>
              <tbody>{inspection.sample_rows.map((values, index) =>
                <tr key={index}>{inspection.observed_columns.map((_, cell) =>
                  <td key={cell}>{values[cell] ?? ''}</td>)}</tr>)}</tbody>
            </table>
          </div>
          <p className="access-hint">
            Соответствие предложено по заголовкам столбцов исходного файла.
            Проверьте тип цены и соответствие каждого столбца.
          </p>
        </>}
        <div className="supplier-pagination">
          <button type="button" className="button button-white"
            disabled={busy || !csrf} onClick={() => void findSaved()}>
            Найти настройки столбцов
          </button>
          <button type="button" className="button button-white"
            disabled={busy} onClick={() => void loadProfileHistory()}>
            История настроек столбцов
          </button>
          {storedMapping?.found && <button type="button" className="button button-white"
            disabled={busy} onClick={useSaved}>
            Применить сохранённые настройки столбцов
          </button>}
          {inspection.stage_mapping && <button type="button"
            className="button button-white" disabled={busy}
            onClick={applyProposal}>Применить предложенные настройки столбцов</button>}
        </div>
        {storedMapping?.found && <p className="access-hint">
          Настройки <code>{storedMapping.profile_sha256}</code> проверены ранее.
          Перед применением снова сверьте столбцы с исходным файлом.
        </p>}
        {storedMapping && !storedMapping.found && <p className="access-hint">
          Не удалось выбрать настройки столбцов: структура могла измениться
          либо существуют конфликтующие проверенные версии. Откройте историю,
          сверьте исходный документ и подтвердите соответствие столбцов заново.
        </p>}
        {profileHistory && <div className="supplier-history" role="group" aria-label="История настроек столбцов">
          <h3>Сохранённые настройки столбцов поставщика</h3>
          {profileHistory.items.length === 0
            ? <p>Сохранённых настроек столбцов пока нет.</p>
            : <ul className="tenant-list">
              {profileHistory.items.map(item => <li key={item.profile_sha256}>
                <span><strong>{new Date(item.reviewed_at).toLocaleString('ru-RU')}</strong>
                  {' · '}{item.price_basis} · <code>{item.profile_sha256}</code>
                  <small>Проверил: {item.approved_by}</small>
                  <small>Исходник SHA-256: {item.approved_source_sha256}</small>
                  <small>Обоснование: {item.review_evidence}</small>
                  {storedMapping?.fingerprint === item.fingerprint &&
                    <small>Структура файла совпадает с сохранёнными настройками столбцов.</small>}
                </span>
              </li>)}
            </ul>}
          {profileHistory.next_offset !== null && <button
            type="button" className="button button-white" disabled={busy}
            onClick={() => void loadProfileHistory(profileHistory.next_offset!)}>
            Показать ещё версии
          </button>}
          {profileHistory.items.length > 1 && <>
            <h3>Сравнение двух версий</h3>
            <div className="supplier-columns">
              <label>Первая версия
                <select value={compareA} onChange={event => setCompareA(event.target.value)}>
                  {profileHistory.items.map(item => <option
                    key={item.profile_sha256} value={item.profile_sha256}>
                    {item.profile_sha256.slice(0, 12)} · {new Date(item.reviewed_at).toLocaleDateString('ru-RU')}
                  </option>)}
                </select>
              </label>
              <label>Вторая версия
                <select value={compareB} onChange={event => setCompareB(event.target.value)}>
                  {profileHistory.items.map(item => <option
                    key={item.profile_sha256} value={item.profile_sha256}>
                    {item.profile_sha256.slice(0, 12)} · {new Date(item.reviewed_at).toLocaleDateString('ru-RU')}
                  </option>)}
                </select>
              </label>
            </div>
            {profileA && profileB && (compareA === compareB
              ? <p>Выберите разные версии.</p>
              : <div className="supplier-inspection-table">
                <table>
                  <thead><tr><th scope="col">Поле</th><th scope="col">Версия 1</th>
                    <th scope="col">Версия 2</th></tr></thead>
                  <tbody>{comparisonFields.filter(field =>
                    compareValue(profileA[field]) !== compareValue(profileB[field]),
                  ).map(field => <tr key={field}><th scope="row">{field}</th>
                    <td>{compareValue(profileA[field])}</td>
                    <td>{compareValue(profileB[field])}</td></tr>)}</tbody>
                </table>
              </div>)}
          </>}
          <p className="access-hint">
            История подтверждает только соответствие столбцов, не цены, остатки
            или пригодность товаров для обучения модели. Применение старой версии
            не выполняется автоматически.
          </p>
        </div>}
      </div>}
      <p className="access-hint">
        Номера столбцов вводятся с 1. Если столбца нет — укажите 0.
        Для XLS/XLSX можно указать точное название листа.
      </p>
      <div className="supplier-columns">
        {([
          ['sku', 'Столбец артикула'],
          ['name', 'Столбец названия'],
          ['price', 'Столбец цены'],
          ['currency', 'Столбец валюты'],
          ['category', 'Столбец категории'],
          ['sale_unit', 'Столбец единицы продажи'],
          ['pieces_per_sale_unit', 'Столбец количества в упаковке'],
          ['header_row', 'Строка заголовка'],
        ] as const).map(([field, label]) => <label key={field}>
          {label}
          <input type="number" min={field === 'name' || field === 'price'
            || field === 'header_row' ? 1 : 0}
            max={field === 'header_row' ? 20 : 64}
            value={columns[field]} required disabled={busy}
            onChange={event => changeColumn(field, event.target.value)}/>
        </label>)}
      </div>
      {columns.currency === 0 && <label className="tenant-checkbox">
        <input type="checkbox" checked={confirmedRub} disabled={busy}
          onChange={event => { invalidate(false); setConfirmedRub(event.target.checked); }}/>
        Подтверждаю: цены исходного файла указаны в RUB
      </label>}
      <label>Лист Excel (если требуется)
        <input type="text" maxLength={100} value={sheet}
          disabled={busy} onChange={event => { invalidate(); setSheet(event.target.value); }}
          placeholder="Например, Прайс-лист"/>
      </label>
      <div className="supplier-columns">
        <label>Разделитель CSV
          <select value={delimiter} disabled={busy}
            onChange={event => { invalidate(false); setDelimiter(event.target.value as typeof delimiter); }}>
            <option value=";">Точка с запятой (;)</option>
            <option value=",">Запятая (,)</option>
            <option value="\t">Табуляция</option>
          </select>
        </label>
        <label>Кодировка CSV
          <select value={encoding} disabled={busy}
            onChange={event => { invalidate(false); setEncoding(event.target.value as typeof encoding); }}>
            <option value="utf-8-sig">UTF-8 / BOM</option>
            <option value="utf-8">UTF-8</option>
            <option value="cp1251">Windows-1251</option>
          </select>
        </label>
      </div>
      <label className="tenant-checkbox">
        <input type="checkbox" checked={quarantine} disabled={busy}
          onChange={event => { invalidate(); setQuarantine(event.target.checked); }}/>
        Отложить строки с формулами Excel на проверку, не вычисляя формулы
      </label>
      <button type="submit" className="button button-white"
        disabled={busy || !csrf || !file || !supplierPattern.test(supplierId)}>
        <FileSearch size={18} aria-hidden="true"/>
        {busy ? 'Анализ файла…' : 'Определить столбцы'}
      </button>
      {inspection && !inspection.issues.includes('sheet_selection_required') &&
        inspection.observed_columns.length > 0 && <label className="tenant-checkbox">
          <input type="checkbox" checked={mappingConfirmed} disabled={busy}
            onChange={event => { setMappingConfirmed(event.target.checked); invalidateReviewOnly(); }}/>
          Я проверил соответствие всех столбцов оригиналу
        </label>}
      {inspection && mappingConfirmed && <div className="supplier-profile-fields">
        <label>Тип цены поставщика
          <select value={priceBasis} disabled={busy}
            onChange={event => {
              setPriceBasis(event.target.value as SupplierPriceBasis);
              invalidateReviewOnly();
            }}>
            <option value="standard">Обычная цена</option>
            <option value="wholesale">Оптовая</option>
            <option value="retail">Розничная</option>
            <option value="rrp">Рекомендованная розничная</option>
            <option value="vat_included">С НДС</option>
          </select>
        </label>
        <label>Что проверено при сопоставлении столбцов
          <textarea rows={2} minLength={8} maxLength={500}
            value={profileEvidence} disabled={busy}
            onChange={event => setProfileEvidence(event.target.value)}
            placeholder="Что именно проверено по исходному документу"/>
        </label>
        <button type="button" className="button button-white"
          disabled={busy || !csrf || profileEvidence.trim().length < 8
            || profileEvidence.trim().length > 500}
          onClick={() => void saveMapping()}>
          Сохранить соответствие столбцов
        </button>
      </div>}
      <button type="button" className="button button-white" onClick={inspectAllRows}
        disabled={busy || !csrf || !file || !inspection || !mappingConfirmed
          || !supplierPattern.test(supplierId)}>
        <FileSearch size={18} aria-hidden="true"/>
        Просмотреть все строки прайса
      </button>
      <button type="button" className="button button-primary" onClick={inspect}
        disabled={busy || !csrf || !file || !inspection || !mappingConfirmed
          || !supplierPattern.test(supplierId)}>
        <FileSearch size={18} aria-hidden="true"/>
        Показать спорные цены
      </button>
    </form>

    {stagePreview && <SupplierStageRows
      preview={stagePreview} busy={busy} canPage={!!file && !!stageMapping}
      onPage={offset => {
        if (file && stageMapping) void showStagePage(offset, stageMapping, file);
      }}
      onSelect={item => {
        setSelectedStageLine(item.line);
        setError('');
        setNotice('');
      }}
    />}
    {stagePreview && stageMapping && file && csrf && onCanonicalReady &&
      <Suspense fallback={<p className="access-hint">Загрузка проверки строк…</p>}>
        <SupplierCanonicalExport
          key={stagePreview.source_sha256 + ':' + JSON.stringify(stageMapping)
            + ':' + priceBasis + ':' + String(quarantine)}
          organizationId={organizationId} supplierId={supplierId} csrf={csrf}
          source={file} mapping={stageMapping} stage={stagePreview}
          priceBasis={priceBasis} quarantine={quarantine} busy={busy}
          onCanonicalReady={onCanonicalReady}
        />
      </Suspense>}
    {chosenStage && stagePreview && file && csrf && stageMapping && <SupplierUnitReviewForm
      key={stagePreview.source_sha256 + ':' + chosenStage.source_row_sha256}
      organizationId={organizationId} supplierId={supplierId} csrf={csrf}
      file={file} sourceSha={stagePreview.source_sha256} mapping={stageMapping}
      quarantine={quarantine} row={chosenStage}
    />}

    {preview && <div className="supplier-preview">
      <div className="supplier-preview-head">
        <strong>Спорных цен: {preview.precision_cases} из {preview.total_rows} строк</strong>
        <span>Источник: {preview.source_filename}</span>
      </div>
      <p className="access-hint">SHA-256 файла: <code>{preview.source_sha256}</code></p>
      {!preview.rows.length && <p>На этой странице спорных цен нет.</p>}
      <ul className="supplier-cases">
        {preview.rows.map(item => <li key={item.source_row_sha256}>
          <button type="button" className="supplier-case"
            aria-pressed={selectedLine === item.line} disabled={busy}
            onClick={() => selectRow(item)}>
            <span><strong>Строка {item.line} — {item.name || 'Нет названия'}</strong>
              <small>{item.supplier_sku || 'Без артикула'}
                {item.source_sale_unit ? ' · ' + item.source_sale_unit : ''}
                {item.source_pack_size ? ' · фасовка ' + item.source_pack_size : ''}
              </small>
            </span>
            <span className="supplier-case-price">{item.source_price_raw}</span>
          </button>
          {(!item.currency_confirmed_rub || item.formula_provenance_unverified) &&
            <span className="supplier-risk">
              Источник требует отдельной проверки:
              {!item.currency_confirmed_rub ? ' валюта' : ''}
              {item.formula_provenance_unverified ? ' формулы' : ''}
            </span>}
          {item.pack_size_review_reason && <span className="supplier-risk">
            {item.pack_size_review_reason} Проверка округления не подтверждает фасовку.
          </span>}
        </li>)}
      </ul>
      <div className="supplier-pagination">
        <button type="button" className="button button-white"
          disabled={busy || !file || !reviewedMapping || preview.offset === 0}
          onClick={() => {
            if (file && reviewedMapping) void showPage(
              Math.max(0, preview.offset - 50), reviewedMapping, file,
            );
          }}>Назад</button>
        <span>Позиции {preview.precision_cases === 0 ? 0 : preview.offset + 1}–{
          preview.offset + preview.rows.length
        } из {preview.precision_cases}</span>
        <button type="button" className="button button-white"
          disabled={busy || !file || !reviewedMapping || preview.next_offset === null}
          onClick={() => {
            if (file && reviewedMapping && preview.next_offset !== null) {
              void showPage(preview.next_offset, reviewedMapping, file);
            }
          }}>Следующие 50</button>
      </div>
    </div>}
    {preview && chosen && <div className="supplier-decision">
      <h3><ShieldCheck size={18} aria-hidden="true"/> Строка {chosen.line}: проверка цены</h3>
      <p>Исходная цена: <strong>{chosen.source_price_raw}</strong>.
        Выберите правило округления и укажите проверенную цену.</p>
      <p className="access-hint">SHA-256 строки: <code>{chosen.source_row_sha256}</code></p>
      {!canApprove && <p className="alert warning">
        Источник требует отдельной проверки. Подтверждение округления заблокировано,
        пока валюта или происхождение формул не подтверждены.
      </p>}
      <form className="access-form" onSubmit={event => void record(event)}>
        <label>Решение по цене
          <select value={finding} disabled={busy} onChange={event => {
            setFinding(event.target.value as SupplierPrecisionFinding);
            setRoundingMode(''); setRoundedPrice(''); setPersonChecked(false);
          }}>
            {Object.entries(REVIEW_LABELS).map(([value, label]) =>
              <option key={value} value={value}
                disabled={value === 'approve_rounding' && !canApprove}>{label}</option>)}
          </select>
        </label>
        {finding === 'approve_rounding' && <>
          <label>Правило округления
            <select value={roundingMode} required disabled={busy}
              onChange={event => setRoundingMode(event.target.value as SupplierRoundingMode)}>
              <option value="">Выберите правило</option>
              <option value="half_up">Половины вверх (half_up)</option>
              <option value="half_even">Половины к чётному (half_even)</option>
              <option value="down">Усечение (down)</option>
              <option value="up">Округление вверх (up)</option>
            </select>
          </label>
          <label>Подтверждённая цена, ₽
            <input type="text" required inputMode="decimal" maxLength={15}
              placeholder="10.13" value={roundedPrice} disabled={busy}
              onChange={event => setRoundedPrice(event.target.value)}/>
          </label>
        </>}
        <label>Обоснование решения
          <textarea rows={3} minLength={32} maxLength={500} required
            value={rationale} disabled={busy}
            onChange={event => setRationale(event.target.value)}
            placeholder="Источник правила, что именно было проверено, и почему принято решение"/>
        </label>
        <label className="tenant-checkbox">
          <input type="checkbox" checked={personChecked} disabled={busy}
            onChange={event => setPersonChecked(event.target.checked)}/>
          Я лично проверил исходную цену и правило
        </label>
        <button className="button button-primary" type="submit"
          disabled={!csrf || busy || !personChecked || rationale.trim().length < 32
            || rationale.trim().length > 500 || (finding === 'approve_rounding'
              && (!canApprove || !roundingMode || !centsPattern.test(roundedPrice)))}>
          {busy ? 'Запись…' : 'Сохранить заключение'}
        </button>
      </form>
      <div className="supplier-history">
        <h3>История решений по строке</h3>
        {history === null ? <p>Загружаем историю проверок…</p> : history === 'unavailable'
          ? <p>История проверок временно недоступна.</p> : history.records.length === 0
          ? <p>Заключений по этой строке пока нет.</p>
          : <ul className="tenant-list">
            {history.records.map(item => <li key={item.reviewer_user_id + item.reviewed_at}>
              <span><strong>{REVIEW_LABELS[item.finding]}</strong>
                {item.rounded_price_rub !== null ? ' · ' + item.rounded_price_rub + ' ₽' : ''}
                {' · '}{new Date(item.reviewed_at).toLocaleString('ru-RU')}
                <small>Пользователь: {item.reviewer_user_id.slice(0, 8)}…</small>
                <small>{item.rationale}</small>
              </span>
            </li>)}
          </ul>}
        <p className="access-hint">Запись проверки не означает публикации товара,
          актуальности цены или подтверждения поставщика.</p>
      </div>
    </div>}
  </section>;
}
