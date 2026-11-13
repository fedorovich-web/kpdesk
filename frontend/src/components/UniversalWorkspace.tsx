import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type { PartyDetails } from '../tenant-session';
import { CompanyInnLookup, formatPartyDetails } from './CompanyInnLookup';
import { UniversalApiError, universalCall, universalPath, universalPost } from '../universal-api';
import type { UniversalCatalog, UniversalContext, UniversalMatches, UniversalNarrative,
  UniversalQuote, UniversalQuoteInput, UniversalRequest, UniversalRequestLine, UniversalRequirement } from '../universal-api';
import '../styles/universal-workspace.css';

type Props = { organizationId: string; organizationName: string; csrf: string | null; role: string };
type EditableRequirement = UniversalRequirement & { value_kind: 'text' | 'number' };
type EditableLine = Omit<UniversalRequestLine, 'requirements'> & { requirements: EditableRequirement[] };
type EditableRequest = { lines: EditableLine[] };
const editableRequest = (request: UniversalRequest): EditableRequest => ({ lines: request.lines.map(line => ({ ...line,
  requirements: line.requirements.map(item => ({ ...item,
    value_kind: typeof item.value === 'number' || item.operator !== 'eq' ? 'number' : 'text',
  })),
})) });
const unavailable = 'Раздел подготовки КП пока недоступен. Обратитесь к администратору KPDesk.';
const emptyLine = (): EditableLine => ({ description: '', source_quote: '', quantity: '', sale_unit: '',
  sku: '', brand: '', model: '', requirements: [] });
// Match the server's Decimal bounds before any conversion to JS Number.
// Quantities remain strings; numeric characteristics retain their JSON number type.
function decimalInput(value: string | number, wholeDigits: number, decimalPlaces: number): string | null {
  const text = String(value).trim().replace(',', '.');
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) return null;
  const negative = text.startsWith('-');
  const [integer = '', fractional = ''] = text.replace(/^[+-]/, '').split('.');
  const whole = integer.replace(/^0+/, '') || '0';
  const fraction = fractional.replace(/0+$/, '');
  if ((whole === '0' ? 0 : whole.length) > wholeDigits || fraction.length > decimalPlaces) return null;
  return (negative ? '-' : '') + whole + (fraction ? '.' + fraction : '');
}
function validQuantity(value: UniversalRequestLine['quantity']): boolean {
  if (!value?.trim()) return true; // Unknown quantity still requires manager review.
  const exact = decimalInput(value, 12, 3);
  return exact !== null && !exact.startsWith('-') && /[1-9]/.test(exact);
}
const narrativeLabels: Record<keyof UniversalNarrative, string> = {
  headline: 'Заголовок', opening: 'Вступление', solution: 'Предложение', closing: 'Завершение',
};
const statusLabel = (status: string) => ({ compatible: 'Проверены указанные требования', incompatible: 'Не соответствует',
  needs_review: 'Требует проверки', manager_selected: 'Выбрано менеджером', unselected: 'Не выбрано',
})[status] ?? 'Требует проверки';
const checkName = (name: string) => ({ sku: 'Артикул', model: 'Модель', brand: 'Бренд', quantity: 'Количество',
  requirements: 'Требования', sale_unit: 'Единица измерения', unit: 'Единица измерения',
})[name] ?? name;
const checkReason = (reason: string) => ({
  sku_verified: 'Артикул совпадает', sku_missing: 'Артикул отсутствует в прайсе', sku_mismatch: 'Артикул не совпадает',
  model_verified: 'Модель совпадает', model_missing: 'Модель отсутствует в прайсе', model_mismatch: 'Модель не совпадает',
  brand_verified: 'Бренд совпадает', brand_missing: 'Бренд отсутствует в прайсе', brand_mismatch: 'Бренд не совпадает',
  spec_verified: 'Характеристика соответствует требованию', spec_missing: 'Характеристика отсутствует в прайсе',
  spec_ambiguous: 'В прайсе есть несколько значений характеристики — уточните нужное',
  spec_type_mismatch: 'Тип значения различается: проверьте, где указано число, а где текст',
  spec_unit_unknown: 'Единица характеристики требует проверки', spec_unit_mismatch: 'Единицы измерения не совпадают',
  spec_mismatch: 'Характеристика не соответствует требованию',
  quantity_verified: 'Количество и единица продажи проверены', quantity_missing: 'Укажите количество в заявке',
  unit_missing: 'Укажите единицу измерения в заявке', unit_mismatch: 'Единицы измерения не совпадают',
  fractional_discrete_quantity: 'Для этой единицы требуется целое количество',
  quantity_precision: 'Количество после пересчёта выходит за допустимый диапазон или содержит больше трёх знаков после запятой',
  quantity_step_mismatch: 'Количество не соответствует шагу продажи из прайса',
  requirement_ambiguous: 'В заявке повторяется характеристика — уточните требования',
})[reason] ?? 'Нужна проверка менеджером';
const errorMessage = (error: unknown) => error instanceof UniversalApiError &&
  (error.code === 'universal_unavailable' || error.code === 'universal_schema_unavailable' || error.code === 'universal_schema_required') ? unavailable
  : error instanceof Error ? error.message : 'Не удалось выполнить операцию.';
const extractionErrorMessage = (error: unknown) => error instanceof UniversalApiError && error.code === 'invalid_output'
  ? 'Распознавание не прошло проверку. Текст заявки остался в поле. Уточните количество и единицы или добавьте позиции вручную.'
  : errorMessage(error);

/** A tenant/authority change unmounts the entire editor, including in-flight guards. */
export function UniversalWorkspace(props: Props) {
  return <UniversalEditor key={JSON.stringify([props.organizationId, props.role, props.csrf])} {...props}/>;
}

function UniversalEditor({ organizationId, organizationName, csrf, role }: Props) {
  const canWrite = (role === 'owner' || role === 'editor') && Boolean(csrf);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [catalog, setCatalog] = useState<UniversalCatalog | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState('');
  const [consent, setConsent] = useState(false);
  const [request, setRequest] = useState<EditableRequest>({ lines: [] });
  const [reviewed, setReviewed] = useState(false);
  const [matches, setMatches] = useState<UniversalMatches | null>(null);
  const [selections, setSelections] = useState<Record<number, string>>({});
  const [context, setContext] = useState<UniversalContext>({ issuer_name: organizationName,
    recipient_name: '', manager_brief: '', commercial_terms: '' });
  const [quotes, setQuotes] = useState<UniversalQuote[]>([]);
  const [quote, setQuote] = useState<UniversalQuote | null>(null);
  const [presentationConsent, setPresentationConsent] = useState(false);
  const [narrative, setNarrative] = useState<UniversalNarrative | null>(null);
  const [narrativeReviewed, setNarrativeReviewed] = useState(false);
  const [operationPending, setPending] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const pending = operationPending || !loaded;
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(true);
  const epoch = useRef(0);
  const busy = useRef(false);
  const keys = useRef(new Map<string, string>());
  const fileKeys = useRef(new WeakMap<File, string>());

  useEffect(() => {
    let active = true;
    alive.current = true;
    void (async () => {
      try {
        const capabilities = await universalCall<{ available: boolean }>(organizationId, '/capabilities');
        if (!active) return;
        setAvailable(capabilities.available);
        if (!capabilities.available) return;
        const [current, history] = await Promise.allSettled([
          universalCall<UniversalCatalog>(organizationId, '/catalog/current'),
          universalCall<{ items: UniversalQuote[] }>(organizationId, '/quotes'),
        ]);
        if (!active) return;
        if (current.status === 'fulfilled') setCatalog(current.value);
        else if (!(current.reason instanceof UniversalApiError && current.reason.status === 404)) setError(errorMessage(current.reason));
        if (history.status === 'fulfilled') setQuotes(history.value.items);
        else setError(errorMessage(history.reason));
      } catch (failure) {
        if (!active) return;
        if (failure instanceof UniversalApiError && failure.status === 404) setAvailable(false);
        else setError(errorMessage(failure));
      } finally { if (active) setLoaded(true); }
    })();
    return () => { active = false; alive.current = false; epoch.current++; };
  }, [organizationId]);

  function clearDerived(clearQuote = true) {
    epoch.current++; busy.current = false; setPending(false);
    setMatches(null); setSelections({}); setNarrative(null); setNarrativeReviewed(false);
    setPresentationConsent(false); setError(''); setNotice('');
    if (clearQuote) setQuote(null);
  }
  function editRequest(next: EditableRequest) {
    clearDerived(); setReviewed(false); setRequest(next);
  }
  function editLine(index: number, patch: Partial<EditableLine>) {
    editRequest({ lines: request.lines.map((line, i) => i === index ? { ...line, ...patch } : line) });
  }
  function editRequirement(index: number, requirementIndex: number, patch: Partial<EditableRequirement>) {
    const line = request.lines[index];
    if (line) editLine(index, { requirements: line.requirements.map((item, i) => i === requirementIndex ? { ...item, ...patch } : item) });
  }
  function idempotency(identity: string) {
    let key = keys.current.get(identity);
    if (!key) { key = crypto.randomUUID(); keys.current.set(identity, key); }
    return key;
  }
  async function run<T>(operation: () => Promise<T>, apply: (value: T) => void,
    describeFailure: (error: unknown) => string = errorMessage) {
    if (busy.current || !loaded) return;
    const version = epoch.current;
    busy.current = true; setPending(true); setError(''); setNotice('');
    try {
      const result = await operation();
      if (alive.current && version === epoch.current) apply(result);
    } catch (failure) {
      if (alive.current && version === epoch.current) setError(describeFailure(failure));
    } finally {
      if (alive.current && version === epoch.current) { busy.current = false; setPending(false); }
    }
  }
  function post<T>(path: string, payload: object) {
    return universalPost<T>(organizationId, path, payload, csrf!, idempotency(path + JSON.stringify(payload)));
  }
  function remember(saved: UniversalQuote) {
    setQuote(saved); setNarrative(null); setNarrativeReviewed(false); setPresentationConsent(false);
    setQuotes(items => [saved, ...items.filter(item => !(item.quote_id === saved.quote_id && item.revision === saved.revision))]);
  }
  function applyParty(kind: 'issuer' | 'recipient', party: PartyDetails) {
    if (!canWrite || pending) return;
    clearDerived();
    setContext(current => kind === 'issuer'
      ? { ...current, issuer_name: party.short_name, issuer_details: party, issuer_contacts: undefined }
      : { ...current, recipient_name: party.short_name, recipient_details: party });
  }
  function useCompanyRequisites() {
    if (!canWrite || pending) return;
    void run(() => sessionApi.companyProfile(organizationId), result => {
      const profile = result.profile;
      if (!profile?.data.issuer_details) {
        setNotice('Сохранённых реквизитов нет. Найдите отправителя по ИНН или заполните раздел «Компания и оформление».');
        return;
      }
      clearDerived();
      setContext(current => ({ ...current, issuer_name: profile.data.display_name,
        issuer_details: profile.data.issuer_details, issuer_contacts: profile.data.contacts || undefined }));
    });
  }
  function importFile() {
    if (!canWrite || !file) return;
    clearDerived();
    const payload = new FormData(); payload.append('file', file);
    // Repeated clicks for the same selected File retain their operation identity.
    let key = fileKeys.current.get(file);
    if (!key) { key = crypto.randomUUID(); fileKeys.current.set(file, key); }
    void run(() => universalCall<UniversalCatalog>(organizationId, '/catalog/import', { method: 'POST', body: payload,
      headers: { 'X-CSRF-Token': csrf!, 'Idempotency-Key': key },
    }), saved => { setCatalog(saved); setReviewed(false); setNotice('Прайс импортирован. Проверьте подбор заново.'); });
  }
  function extract() {
    if (!canWrite || !catalog || !consent || !source.trim()) return;
    clearDerived();
    void run(() => post<{ request: UniversalRequest }>('/requests/extract', { catalog_version_id: catalog.version_id,
      text: source, consent_to_external_processing: true }), extracted => { setRequest(editableRequest(extracted.request)); setReviewed(false); }, extractionErrorMessage);
  }
  const requestValid = request.lines.length > 0 && request.lines.length <= 20 && request.lines.every(line =>
    Boolean(line.description.trim() && line.source_quote.trim()) && validQuantity(line.quantity) && line.requirements.every(item =>
      Boolean(item.name.trim() && String(item.value).trim() && item.source_quote.trim()) && (item.value_kind !== 'number'
        || decimalInput(item.value, 9, 6) !== null)));
  function submittedRequest(): UniversalRequest {
    return { lines: request.lines.map(line => ({ ...line,
      quantity: line.quantity?.trim() ? decimalInput(line.quantity, 12, 3) : null, sale_unit: line.sale_unit || null,
      sku: line.sku || null, brand: line.brand || null, model: line.model || null,
      requirements: line.requirements.map(({ value_kind, ...item }) => ({ ...item, unit: item.unit || null,
        value: value_kind === 'number' ? Number(decimalInput(item.value, 9, 6)) : String(item.value),
      })),
    })) };
  }
  function preview() {
    if (!canWrite || !catalog || !reviewed || !requestValid) return;
    clearDerived();
    void run(() => post<UniversalMatches>('/matches/preview', { catalog_version_id: catalog.version_id, request: submittedRequest() }), setMatches);
  }
  const allSelected = request.lines.length > 0 && request.lines.every((_, i) => Boolean(selections[i + 1]));
  function save() {
    if (!canWrite || !catalog || !requestValid || !reviewed || !matches || !allSelected || !context.issuer_name.trim()) return;
    const payload: UniversalQuoteInput = { catalog_version_id: catalog.version_id, request: submittedRequest(), ...context,
      selections: request.lines.map((_, i) => ({ line_number: i + 1, sku: selections[i + 1]! })) };
    void run(() => post<UniversalQuote>('/quotes', payload), remember);
  }
  function openQuote(item: UniversalQuote) {
    clearDerived();
    void run(() => universalCall<UniversalQuote>(organizationId,
      '/quotes/' + encodeURIComponent(item.quote_id) + '?revision=' + item.revision), remember);
  }
  function generatePresentation() {
    if (!canWrite || !quote || !presentationConsent) return;
    setNarrative(null); setNarrativeReviewed(false);
    void run(() => post<{ narrative: UniversalNarrative; quote_id: string; source_revision: number; source_snapshot_sha256: string }>(
      '/quotes/' + encodeURIComponent(quote.quote_id) + '/presentation', {
      expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256, consent_to_external_processing: true,
    }), result => {
      if (result.quote_id !== quote.quote_id || result.source_revision !== quote.revision || result.source_snapshot_sha256 !== quote.snapshot_sha256) {
        throw new Error('Текст предложения не соответствует открытой версии КП. Откройте КП заново.');
      }
      setNarrative(result.narrative);
    });
  }
  function savePresentation() {
    if (!canWrite || !quote || !narrative || !narrativeReviewed) return;
    const payload = { catalog_version_id: quote.catalog_version_id, request: quote.source_request, selections: quote.selections,
      issuer_name: quote.issuer_name, recipient_name: quote.recipient_name, manager_brief: quote.manager_brief,
      issuer_details: quote.issuer_details, recipient_details: quote.recipient_details, issuer_contacts: quote.issuer_contacts,
      commercial_terms: quote.commercial_terms, expected_revision: quote.revision,
      source_snapshot_sha256: quote.snapshot_sha256, narrative, reviewed: true };
    void run(() => post<UniversalQuote>('/quotes/' + encodeURIComponent(quote.quote_id) + '/revisions', payload), remember);
  }

  return <section className="universal-workspace" aria-label="Подготовка КП">
    <p>Загрузите прайс, добавьте заявку и выберите товары для предложения. Стоимость рассчитается по вашему прайсу.</p>
    {error && <p className="alert error" role="alert">{error}</p>}
    {notice && <p className="alert success" role="status">{notice}</p>}
    {pending && <p role="status">Обрабатываем запрос…</p>}
    {available === null && !error && <p role="status">Загружаем данные…</p>}
    {available === false && <p role="status">{unavailable}</p>}
    {available === true && <>
      <section aria-label="Прайс" className="universal-step">
        <h3>1. Прайс</h3>
        <p>Загрузите товары, цены и единицы продажи из CSV или Excel. Категорию можно не указывать.
          Названия обязательных столбцов: sku;name;price_rub;sale_unit. Дополнительные столбцы станут характеристиками.
          Если цена указана за 100 единиц, добавьте price_quantity=100. Для нестандартной единицы укажите шаг продажи в quantity_step.</p>
        {catalog ? <><p>Товаров в прайсе: {catalog.products.length}. Версия: {catalog.version_id}</p>
          <details><summary>Посмотреть товары и характеристики</summary><div className="universal-catalog-list">
            {catalog.products.map(product => <p key={product.sku}><strong>{product.sku} · {product.name}</strong>
              {' — '}{product.price_rub} ₽ за {product.price_quantity} {product.sale_unit}
              {product.category && <> · {product.category}</>}
              {product.specifications.map((item, i) => <span key={i}> · {item.name}: {item.value} {item.unit ?? ''}</span>)}</p>)}
          </div></details></> : <p>Прайс пока не загружен.</p>}
        {canWrite && <div className="access-form">
          <label>Прайс CSV или XLSX<input type="file" accept=".csv,.xlsx" onChange={event => {
            clearDerived(); setFile(event.target.files?.[0] ?? null);
          }}/></label>
          <button type="button" className="button button-white" disabled={!file || pending} onClick={importFile}>Импортировать прайс</button>
        </div>}
      </section>
      {canWrite && <>
        <section className="universal-step" aria-label="Заявка">
          <h3>2. Заявка и требования</h3>
          <div className="access-form"><label>Текст заявки<textarea rows={4} maxLength={12000} value={source}
            onChange={event => { clearDerived(); setSource(event.target.value); setRequest({ lines: [] }); setReviewed(false); setConsent(false); }}/></label>
            <label className="tenant-checkbox"><input type="checkbox" checked={consent} onChange={event => {
              epoch.current++; busy.current = false; setPending(false); setConsent(event.target.checked);
            }}/>Разрешаю передать заявку внешнему ИИ для распознавания</label>
            <div className="universal-actions"><button type="button" className="button button-white" disabled={!catalog || !consent || !source.trim() || pending}
              onClick={extract}>Распознать заявку</button>
              <button type="button" className="button button-white" disabled={request.lines.length >= 20 || pending}
                onClick={() => editRequest({ lines: [...request.lines, emptyLine()] })}>Добавить позицию вручную</button></div>
          </div>
          {request.lines.map((line, index) => <fieldset className="universal-line" key={index}><legend>Позиция {index + 1}</legend>
            <div className="universal-fields">{([
              ['description', 'Описание'], ['source_quote', 'Цитата из заявки'], ['quantity', 'Количество'], ['sale_unit', 'Единица'],
              ['sku', 'Артикул'], ['brand', 'Бренд'], ['model', 'Модель'],
            ] as const).map(([field, label]) => <label key={field}>{label}<input aria-label={`${label} позиции ${index + 1}`}
              inputMode={field === 'quantity' ? 'decimal' : undefined} value={line[field] ?? ''}
              aria-invalid={field === 'quantity' ? !validQuantity(line.quantity) : undefined}
              onChange={event => editLine(index, { [field]: event.target.value })}/></label>)}</div>
            {line.requirements.map((requirement, i) => <div key={i} className="universal-requirement">
              <label>Характеристика<input aria-label={`Название характеристики ${i + 1} позиции ${index + 1}`} value={requirement.name}
                onChange={event => editRequirement(index, i, { name: event.target.value })}/></label>
              <label>Условие<select aria-label={`Условие характеристики ${i + 1} позиции ${index + 1}`} value={requirement.operator}
                onChange={event => editRequirement(index, i, { operator: event.target.value as UniversalRequirement['operator'],
                  value_kind: event.target.value === 'eq' ? requirement.value_kind : 'number' })}>
                <option value="eq">Равно</option><option value="min">Не менее</option><option value="max">Не более</option></select></label>
              <label>Тип значения<select aria-label={`Тип значения характеристики ${i + 1} позиции ${index + 1}`}
                value={requirement.value_kind} disabled={requirement.operator !== 'eq'}
                onChange={event => editRequirement(index, i, { value_kind: event.target.value as EditableRequirement['value_kind'] })}>
                <option value="text">Текст или код</option><option value="number">Число</option></select></label>
              <label>Значение<input aria-label={`Значение характеристики ${i + 1} позиции ${index + 1}`} value={requirement.value}
                aria-invalid={requirement.value_kind === 'number' && decimalInput(requirement.value, 9, 6) === null}
                onChange={event => editRequirement(index, i, { value: event.target.value })}/></label>
              <label>Единица<input aria-label={`Единица характеристики ${i + 1} позиции ${index + 1}`} value={requirement.unit ?? ''}
                onChange={event => editRequirement(index, i, { unit: event.target.value })}/></label>
              <label>Цитата<input aria-label={`Цитата характеристики ${i + 1} позиции ${index + 1}`} value={requirement.source_quote}
                onChange={event => editRequirement(index, i, { source_quote: event.target.value })}/></label>
              <button type="button" className="button button-white" aria-label={`Удалить характеристику ${i + 1} позиции ${index + 1}`}
                onClick={() => editLine(index, { requirements: line.requirements.filter((_, j) => j !== i) })}>Удалить</button>
            </div>)}
            <div className="universal-actions"><button type="button" className="button button-white" onClick={() => editLine(index,
              { requirements: [...line.requirements, { name: '', value: '', unit: '', operator: 'eq', source_quote: '', value_kind: 'text' }] })}>Добавить характеристику позиции {index + 1}</button>
              <button type="button" className="button button-white" onClick={() => editRequest({ lines: request.lines.filter((_, i) => i !== index) })}>Удалить позицию {index + 1}</button></div>
          </fieldset>)}
          {request.lines.some(line => !validQuantity(line.quantity)) && <p role="alert">
            Проверьте количество: нужно положительное число, до 12 цифр до разделителя и до 3 после. Можно использовать точку или запятую.
          </p>}
          {request.lines.some(line => line.requirements.some(item => item.value_kind === 'number'
            && decimalInput(item.value, 9, 6) === null)) && <p role="alert">
            Проверьте числовые характеристики: допустимо до 9 цифр до разделителя и до 6 после. Можно использовать точку или запятую.
          </p>}
          <label className="tenant-checkbox"><input type="checkbox" checked={reviewed} disabled={!requestValid} onChange={event => {
            clearDerived(); setReviewed(event.target.checked);
          }}/>Проверил все позиции и характеристики</label>
        </section>
        <section className="universal-step" aria-label="Подбор и условия КП"><h3>3. Подбор товаров и условия КП</h3>
          <div className="universal-fields">{([['issuer_name', 'Название вашей компании'], ['recipient_name', 'Получатель'],
            ['manager_brief', 'Задача клиента и пожелания к тексту'], ['commercial_terms', 'Условия оплаты и поставки']] as const).map(([field, label]) =>
            <label key={field}>{label}<textarea rows={2} value={context[field] ?? ''} onChange={event => {
              clearDerived(); setContext({ ...context, [field]: event.target.value,
                ...(field === 'issuer_name' ? { issuer_details: undefined, issuer_contacts: undefined } : {}),
                ...(field === 'recipient_name' ? { recipient_details: undefined } : {}) });
            }}/></label>)}</div>
          <button type="button" className="button button-white" disabled={!canWrite || pending}
            onClick={useCompanyRequisites}>Использовать реквизиты компании</button>
          <p>Проверьте реквизиты перед сохранением. Они войдут в версию КП и PDF, без передачи генератору текста.</p>
          <details className="company-requisites-confirmed"><summary>Реквизиты отправителя</summary>
            <CompanyInnLookup organizationId={organizationId} csrf={csrf} disabled={!canWrite || pending}
              onApply={party => applyParty('issuer', party)}/>
            {context.issuer_details && <><pre>{formatPartyDetails(context.issuer_details)}</pre>
              {context.issuer_contacts && <p>{context.issuer_contacts}</p>}
              <button type="button" className="text-button" disabled={!canWrite || pending} onClick={() => {
                clearDerived(); setContext(current => ({ ...current, issuer_details: undefined, issuer_contacts: undefined }));
              }}>Убрать реквизиты отправителя</button></>}
          </details>
          <details className="company-requisites-confirmed"><summary>Реквизиты заказчика</summary>
            <CompanyInnLookup organizationId={organizationId} csrf={csrf} disabled={!canWrite || pending}
              onApply={party => applyParty('recipient', party)}/>
            {context.recipient_details && <><pre>{formatPartyDetails(context.recipient_details)}</pre>
              <button type="button" className="text-button" disabled={!canWrite || pending} onClick={() => {
                clearDerived(); setContext(current => ({ ...current, recipient_details: undefined }));
              }}>Убрать реквизиты заказчика</button></>}
          </details>
          <button type="button" className="button button-white" disabled={!catalog || !reviewed || !requestValid || pending} onClick={preview}>Подобрать товары</button>
          {matches && <div><p>Выберите товар отдельно для каждой позиции. Сверьте характеристики каждого товара с заявкой.</p>
            {matches.lines.map(line => <fieldset className="universal-line" key={line.line_number}><legend>Подбор для позиции {line.line_number}</legend>
              {line.candidates.length === 0 && <p>Для этой позиции не удалось подобрать товары. Проверьте требования и данные прайса.</p>}
              {line.candidates.map(candidate => <div className="universal-candidate" key={candidate.sku}>
                <label className="tenant-checkbox"><input type="radio" name={`universal-selection-${line.line_number}`} aria-label={`Выбрать ${candidate.sku} для позиции ${line.line_number}`}
                  checked={selections[line.line_number] === candidate.sku} disabled={candidate.status === 'incompatible' || pending}
                  onChange={() => { setSelections({ ...selections, [line.line_number]: candidate.sku }); setQuote(null); setNarrative(null); }}/>
                  <span><strong>{candidate.sku} · {candidate.name}</strong> · {statusLabel(candidate.status)}</span></label>
                {candidate.checks.map((check, i) => <p key={i}>{checkName(check.name)}: {statusLabel(check.status)}. {checkReason(check.reason)}</p>)}
              </div>)}
            </fieldset>)}
          </div>}
          <button type="button" className="button button-primary" disabled={!catalog || !matches || !reviewed || !allSelected || !context.issuer_name.trim() || pending}
            onClick={save}>Сохранить КП</button>
        </section>
      </>}
      <section className="universal-step" aria-label="История КП"><h3>Сохранённые КП</h3>
        {quotes.length ? <ul className="universal-history">{quotes.map(item => <li key={`${item.quote_id}:${item.revision}`}>
          <button type="button" className="button button-white" disabled={pending} onClick={() => openQuote(item)}>Открыть КП {item.quote_id}, версия {item.revision}</button>
        </li>)}</ul> : <p>Сохранённых КП пока нет.</p>}
      </section>
      {quote && <section className="universal-step" aria-label="Сохранённый черновик"><h3>Черновик КП · версия {quote.revision}</h3>
        <p>Не утверждено. {quote.ready_for_draft ? 'Стоимость рассчитана по прайсу. Проверьте состав и условия перед использованием документа.' : 'Есть позиции, требующие уточнения.'}</p>
        {quote.lines.map(line => <p key={line.line_number}>{line.line_number}. {line.name ?? line.sku ?? 'Товар не выбран'}
          {' · '}{line.sale_quantity ?? '—'} {line.sale_unit ?? ''} · {line.line_total_rub ?? '—'} ₽ · {statusLabel(line.status)}</p>)}
        <p>Итого: {quote.total_rub ?? 'не рассчитано'}{quote.total_rub !== null ? ' ₽' : ''}</p>
        <details><summary>Исходная заявка и условия этой версии</summary>
          <p>От: {quote.issuer_name}{quote.recipient_name ? ` · Для: ${quote.recipient_name}` : ''}</p>
          {quote.issuer_details && <><h4>Реквизиты отправителя</h4><pre>{formatPartyDetails(quote.issuer_details)}</pre></>}
          {quote.issuer_contacts && <p>{quote.issuer_contacts}</p>}
          {quote.recipient_details && <><h4>Реквизиты заказчика</h4><pre>{formatPartyDetails(quote.recipient_details)}</pre></>}
          {quote.manager_brief && <p>{quote.manager_brief}</p>}
          {quote.commercial_terms && <p>{quote.commercial_terms}</p>}
          {quote.source_request.lines.map((line, i) => <div key={i}>
            <p>{i + 1}. {line.description} · {line.quantity ?? 'Количество не указано'} {line.sale_unit ?? ''}</p>
            <blockquote>{line.source_quote}</blockquote>
            {line.requirements.map((requirement, j) => <p key={j}>{requirement.name}: {
              requirement.operator === 'min' ? 'не менее' : requirement.operator === 'max' ? 'не более' : 'равно'
            } {requirement.value} {requirement.unit ?? ''} · «{requirement.source_quote}»</p>)}
          </div>)}
        </details>
        {quote.narrative && <div>{Object.keys(narrativeLabels).map(key => <p key={key}>{quote.narrative![key as keyof UniversalNarrative]}</p>)}</div>}
        <a className="button button-white" href={universalPath(organizationId, '/quotes/' + encodeURIComponent(quote.quote_id) + '/pdf?revision=' + quote.revision)}>Скачать PDF черновика</a>
        {canWrite && quote.ready_for_draft && <div className="access-form">
          <label className="tenant-checkbox"><input type="checkbox" checked={presentationConsent} onChange={event => {
            epoch.current++; busy.current = false; setPending(false); setPresentationConsent(event.target.checked); setNarrative(null); setNarrativeReviewed(false);
          }}/>Разрешаю передать факты этого КП внешнему ИИ для подготовки текста</label>
          <button type="button" className="button button-white" disabled={!presentationConsent || pending} onClick={generatePresentation}>Сгенерировать текст КП</button>
          {narrative && <><p>Проверьте текст. Сохранение создаст новую версию черновика и не утвердит документ.</p>
            {(Object.keys(narrativeLabels) as Array<keyof UniversalNarrative>).map(field => <label key={field}>{narrativeLabels[field]}
              <textarea rows={3} value={narrative[field]} onChange={event => {
                epoch.current++; busy.current = false; setPending(false); setNarrative({ ...narrative, [field]: event.target.value }); setNarrativeReviewed(false);
              }}/></label>)}
            <label className="tenant-checkbox"><input type="checkbox" checked={narrativeReviewed} onChange={event => setNarrativeReviewed(event.target.checked)}/>
              Проверил текст предложения и все факты</label>
            <button type="button" className="button button-primary" disabled={!narrativeReviewed || pending} onClick={savePresentation}>Сохранить новую версию с текстом</button>
          </>}
        </div>}
        {canWrite && quote.ready_for_draft && <UniversalFeedback key={`${quote.quote_id}:${quote.revision}:${quote.snapshot_sha256}`}
          organizationId={organizationId} csrf={csrf!} quote={quote}/>}
      </section>}
    </>}
  </section>;
}

function UniversalFeedback({ organizationId, csrf, quote }: { organizationId: string; csrf: string; quote: UniversalQuote }) {
  type SavedFeedback = { id: string; revision: number; curation_status: string };
  const [ratings, setRatings] = useState<Record<number, string>>({});
  const [reviewed, setReviewed] = useState(false);
  const [consent, setConsent] = useState(false);
  const [feedbackId, setFeedbackId] = useState<string | null>(null);
  const [operationPending, setPending] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [history, setHistory] = useState<SavedFeedback[]>([]);
  const pending = operationPending || historyLoading;
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(true);
  const busy = useRef(false);
  const keys = useRef(new Map<string, string>());
  useEffect(() => {
    let active = true; alive.current = true;
    void (async () => {
      try {
        const items: SavedFeedback[] = [];
        let offset: number | null = 0;
        while (offset !== null) {
          const page: { items: SavedFeedback[]; next_offset: number | null } = await universalCall(organizationId,
            '/quotes/' + encodeURIComponent(quote.quote_id) + '/feedback?offset=' + offset);
          if (!active) return;
          if (!Array.isArray(page.items) || (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset)
            || page.next_offset <= offset || page.next_offset > 10000))) {
            throw new Error('Не удалось загрузить сохранённые оценки. Откройте КП заново.');
          }
          items.push(...page.items); offset = page.next_offset;
        }
        setHistory(items);
      } catch (failure) { if (active) setError(errorMessage(failure)); }
      finally { if (active) setHistoryLoading(false); }
    })();
    return () => { active = false; alive.current = false; };
  }, [organizationId, quote.quote_id]);
  const complete = quote.lines.every(line => line.sku && ['positive', 'negative'].includes(ratings[line.line_number] ?? ''));
  async function submit() {
    if (!complete || !reviewed || !consent || busy.current || historyLoading) return;
    const payload = { expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256, reviewed: true,
      training_consent: true, decisions: quote.lines.map(line => ({ line_number: line.line_number,
        relevant_sku: ratings[line.line_number] === 'positive' ? line.sku : null,
        negative_skus: ratings[line.line_number] === 'negative' ? [line.sku] : [],
      })) };
    const identity = JSON.stringify(payload);
    let key = keys.current.get(identity);
    if (!key) { key = crypto.randomUUID(); keys.current.set(identity, key); }
    busy.current = true; setPending(true); setError('');
    try {
      const result = await universalPost<{ id: string }>(organizationId,
        '/quotes/' + encodeURIComponent(quote.quote_id) + '/feedback', payload, csrf, key);
      if (alive.current) {
        setFeedbackId(result.id);
        setHistory(items => [{ id: result.id, revision: quote.revision, curation_status: 'pending_review' }, ...items.filter(item => item.id !== result.id)]);
        setNotice('Оценка сохранена для проверки. Обучение автоматически не запускается.');
      }
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) { busy.current = false; setPending(false); } }
  }
  async function withdraw(id: string) {
    if (busy.current || historyLoading) return;
    busy.current = true; setPending(true); setError('');
    try {
      await universalCall(organizationId, '/feedback/' + encodeURIComponent(id), {
        method: 'DELETE', headers: { 'X-CSRF-Token': csrf },
      });
      if (alive.current) {
        setHistory(items => items.filter(item => item.id !== id));
        if (feedbackId === id) { setFeedbackId(null); setRatings({}); setReviewed(false); setConsent(false); keys.current.clear(); }
        setNotice('Оценка отозвана.');
      }
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) { busy.current = false; setPending(false); } }
  }
  async function downloadFeedback() {
    if (busy.current) return;
    busy.current = true; setPending(true); setError('');
    try {
      const items: unknown[] = [];
      let offset: number | null = 0;
      while (offset !== null) {
        const page: { items: unknown[]; next_offset: number | null } = await universalCall(organizationId,
          '/feedback/export?offset=' + offset);
        if (!alive.current) return;
        if (!Array.isArray(page.items) || (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset)
          || page.next_offset <= offset || page.next_offset > 10000))) {
          throw new Error('Не удалось скачать все оценки. Повторите попытку позже.');
        }
        items.push(...page.items); offset = page.next_offset;
      }
      const file = new Blob([JSON.stringify({ format: 'kpdesk-universal-feedback-v1', organization_id: organizationId,
        training_activated: false, items }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(file);
      try {
        const link = document.createElement('a'); link.href = url; link.download = 'kpdesk-feedback.json'; link.click();
      } finally { URL.revokeObjectURL(url); }
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) { busy.current = false; setPending(false); } }
  }
  return <section className="universal-step" aria-label="Оценка подбора"><h4>Оценка подбора</h4>
    <p>Укажите, подходит ли каждый выбранный товар к заявке. Оценки сохранятся для отдельной проверки.
      Обучение не запускается, а действующий подбор не меняется автоматически.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!feedbackId && <>
      {quote.lines.map(line => <label key={line.line_number}>{line.name ?? line.sku}
        <select aria-label={`Оценка товара позиции ${line.line_number}`} value={ratings[line.line_number] ?? ''} disabled={pending}
          onChange={event => { setRatings({ ...ratings, [line.line_number]: event.target.value }); setReviewed(false); }}>
          <option value="">Выберите оценку</option><option value="positive">Выбранный товар подходит</option>
          <option value="negative">Выбранный товар не подходит</option>
        </select></label>)}
      <label className="tenant-checkbox"><input type="checkbox" checked={reviewed} disabled={pending}
        onChange={event => setReviewed(event.target.checked)}/>Проверил оценку подбора</label>
      <label className="tenant-checkbox"><input type="checkbox" checked={consent} disabled={pending}
        onChange={event => setConsent(event.target.checked)}/>Разрешаю использовать эти оценки для подготовки обучающих данных</label>
      <button type="button" className="button button-white" disabled={!complete || !reviewed || !consent || pending} onClick={() => void submit()}>Сохранить оценку подбора</button>
    </>}
    {historyLoading && <p role="status">Загружаем сохранённые оценки…</p>}
    {history.map(item => <div key={item.id} className="universal-actions">
      <span>Оценка подбора · версия {item.revision} · ожидает проверки</span>
      <button type="button" className="button button-white" disabled={pending}
        aria-label={item.id === feedbackId ? 'Отозвать эту оценку' : `Отозвать оценку ${item.id}, версия ${item.revision}`}
        onClick={() => void withdraw(item.id)}>{item.id === feedbackId ? 'Отозвать эту оценку' : 'Отозвать оценку'}</button>
    </div>)}
    <button type="button" className="button button-white" disabled={pending} onClick={() => void downloadFeedback()}>Скачать оценки подбора</button>
  </section>;
}
