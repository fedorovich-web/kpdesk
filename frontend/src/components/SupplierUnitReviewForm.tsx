import { useState } from 'react';
import type { FormEvent } from 'react';
import { sessionApi } from '../tenant-session';
import type {
  SupplierPrecisionMapping, SupplierStageRow, SupplierUnitFinding,
  SupplierUnitHistory,
} from '../tenant-session';

type Props = {
  organizationId: string; supplierId: string; csrf: string;
  file: File; sourceSha: string; mapping: SupplierPrecisionMapping;
  quarantine: boolean; row: SupplierStageRow;
};

export function SupplierUnitReviewForm({
  organizationId, supplierId, csrf, file, sourceSha, mapping, quarantine, row,
}: Props) {
  const [finding, setFinding] = useState<SupplierUnitFinding>('needs_supplier_confirmation');
  const [unit, setUnit] = useState<'штука' | 'пачка' | 'коробка'>('штука');
  const [pack, setPack] = useState('1');
  const [rationale, setRationale] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [history, setHistory] = useState<SupplierUnitHistory | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const forbidden = [
    'unconfirmed_or_non_rub_currency',
    'xlsx_formula_cell_quarantined',
    'xls_formula_and_macro_provenance_unverified',
  ];
  const canApprove = (
    row.price_rub !== null || row.issues.includes('price_precision_requires_review')
  ) && !row.issues.some(code => forbidden.includes(code));
  const validPack = /^[1-9][0-9]{0,5}$/.test(pack) && Number(pack) <= 100000;
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!confirmed || !csrf || rationale.trim().length < 32
      || rationale.trim().length > 500
      || (finding === 'confirm_price_unit' && (!canApprove || !validPack))) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const response = await sessionApi.attestSupplierUnit(
        organizationId, supplierId, file, mapping, sourceSha, {
          source_row_sha256: row.source_row_sha256, line: row.line,
          finding,
          approved_sale_unit: finding === 'confirm_price_unit' ? unit : null,
          approved_pack_size: finding === 'confirm_price_unit' ? Number(pack) : null,
          rationale: rationale.trim(),
        }, csrf, quarantine,
      );
      if (!response.recorded || response.published || response.model_training_authorized
        || response.source_sha256 !== sourceSha
        || response.source_row_sha256 !== row.source_row_sha256
        || response.line !== row.line) {
        throw new Error('Не удалось подтвердить сохранение проверки для этой строки. Обновите историю.');
      }
      setConfirmed(false);
      setRationale('');
      setHistory(null);
      setNotice('Заключение сохранено. Прайс и действующая модель подбора не изменены.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось записать заключение.');
    } finally {
      setBusy(false);
    }
  };
  const loadHistory = async (offset = 0) => {
    setBusy(true);
    setError('');
    try {
      const response = await sessionApi.supplierUnitHistory(
        organizationId, supplierId, sourceSha, offset,
      );
      if (response.published || response.model_training_authorized) {
        throw new Error('Не удалось подтвердить историю проверки единиц. Проверьте текущий прайс перед продолжением.');
      }
      setHistory(response);
      setHistoryOffset(offset);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось прочитать историю.');
    } finally {
      setBusy(false);
    }
  };
  return <section className="supplier-decision" aria-label="Проверка единицы цены">
    <h3>Строка {row.line}: подтверждение единицы цены</h3>
    <p className="access-hint">
      Исходник: <code>{sourceSha}</code>. Строка: <code>{row.source_row_sha256}</code>.
      Источник: {row.source_sale_unit ?? 'единица не указана'};
      упаковка: {row.source_pack_size ?? 'не указана или некорректна'}.
    </p>
    {row.pack_size_review_reason && <p className="supplier-risk">
      {row.pack_size_review_reason}
    </p>}
    <p className="access-hint">
      Подтверждение означает личную проверку единицы, за которую выставлена цена,
      а не публикацию товара, проверку наличия или подготовку данных для обучения.
    </p>
    {error && <p className="alert error" role="alert">{error}</p>}
    {notice && <p className="alert success" role="status">{notice}</p>}
    <form className="access-form" onSubmit={event => void submit(event)}>
      <label>Заключение по фасовке
        <select value={finding} disabled={busy}
          onChange={event => { setFinding(event.target.value as SupplierUnitFinding); setConfirmed(false); }}>
          <option value="needs_supplier_confirmation">Требуется ответ поставщика</option>
          <option value="reject_source_unit">Отклонить единицу из файла</option>
          <option value="confirm_price_unit" disabled={!canApprove}>
            Подтвердить единицу цены после проверки
          </option>
        </select>
      </label>
      {finding === 'confirm_price_unit' && <>
        <label>Подтверждённая единица цены
          <select value={unit} disabled={busy}
            onChange={event => { setUnit(event.target.value as typeof unit); setConfirmed(false); }}>
            <option value="штука">Штука</option>
            <option value="пачка">Пачка</option>
            <option value="коробка">Коробка</option>
          </select>
        </label>
        <label>Штук в единице продажи
          <input type="number" required min={1} max={100000} step={1}
            value={pack} disabled={busy}
            onChange={event => { setPack(event.target.value); setConfirmed(false); }}/>
        </label>
      </>}
      <label>Что подтверждено по документу поставщика
        <textarea rows={3} required minLength={32} maxLength={500}
          value={rationale} disabled={busy}
          onChange={event => { setRationale(event.target.value); setConfirmed(false); }}
          placeholder="Укажите, какой документ поставщика лично проверен и что подтверждено"/>
      </label>
      <label className="tenant-checkbox">
        <input type="checkbox" checked={confirmed} disabled={busy}
          onChange={event => setConfirmed(event.target.checked)}/>
        Я лично сверил оригинал и подтверждаю достоверность этого заключения
      </label>
      <button type="submit" className="button button-primary"
        disabled={busy || !confirmed || rationale.trim().length < 32
          || rationale.trim().length > 500
          || (finding === 'confirm_price_unit' && (!canApprove || !validPack))}>
        {busy ? 'Запись…' : 'Сохранить проверку единицы цены'}
      </button>
    </form>
    <div className="supplier-history">
      <button type="button" className="button button-white" disabled={busy}
        onClick={() => void loadHistory()}>
        История подтверждений этого файла
      </button>
      {history && <>
        <p className="access-hint">
          Записи {historyOffset + 1}–{historyOffset + history.records.length},
          все строки выбранного файла (не только текущая).
        </p>
        <ul className="tenant-list">
          {history.records.map(item => <li key={item.source_line + ':' + item.reviewer_user_id}>
            <span><strong>Строка {item.source_line}: {item.finding}</strong>
              {item.approved_sale_unit ? ' · ' + item.approved_sale_unit : ''}
              {item.approved_pack_size !== null ? ' · ' + item.approved_pack_size + ' шт.' : ''}
              <small>{item.reviewer_user_id.slice(0, 8)} · {
                new Date(item.reviewed_at).toLocaleString('ru-RU')
              }</small>
              <small>{item.rationale}</small>
            </span>
          </li>)}
        </ul>
        {history.next_offset !== null && <button type="button"
          className="button button-white" disabled={busy}
          onClick={() => { if (history.next_offset !== null) void loadHistory(history.next_offset); }}>
          Следующая страница истории
        </button>}
      </>}
    </div>
  </section>;
}
