import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type {
  HistoricalSupplierSourceRef, SupplierCatalogDiff, SupplierSourceRef,
} from '../tenant-session';

type Source = SupplierSourceRef | HistoricalSupplierSourceRef;
type Option = { ref: Source; label: string };
type Props = {
  organizationId: string; supplierId: string; csrf: string;
  options: Option[];
  auditOnly?: boolean;
};
const key = (source: Source) =>
  ('profile_sha256' in source ? source.profile_sha256 : source.profile_fingerprint)
  + '|' + source.catalog_version_id;
const LABELS: Record<string, string> = {
  added: 'Товар добавлен', removed: 'Товар удалён',
  price_changed: 'Изменилась цена при неизменных характеристиках',
  product_changed: 'Изменились характеристики',
  product_and_price_changed: 'Изменились цена и характеристики',
};

export function SupplierCatalogVersionDiff({
  organizationId, supplierId, csrf, options, auditOnly = false,
}: Props) {
  const [baseline, setBaseline] = useState('');
  const [target, setTarget] = useState('');
  const [report, setReport] = useState<SupplierCatalogDiff | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  useEffect(() => () => { generation.current++; }, []);
  const reset = () => {
    generation.current++;
    setReport(null); setError(''); setBusy(false);
  };
  const request = async (offset: number) => {
    const before = options.find(item => key(item.ref) === baseline)?.ref;
    const after = options.find(item => key(item.ref) === target)?.ref;
    if (!before || !after || before.catalog_version_id === after.catalog_version_id) {
      setError('Выберите две разные подтверждённые версии одного поставщика.');
      return;
    }
    const requestId = ++generation.current;
    setError(''); setBusy(true);
    try {
      let result: SupplierCatalogDiff;
      if (auditOnly) {
        if (!('profile_sha256' in before && 'profile_sha256' in after))
          throw new Error('Исторический источник некорректен.');
        result = await sessionApi.compareHistoricalSupplierVersions(
          organizationId, before, after, csrf, offset,
        );
      } else {
        if (!('profile_fingerprint' in before && 'profile_fingerprint' in after))
          throw new Error('Источник текущего прайса некорректен.');
        result = await sessionApi.compareSupplierVersions(
          organizationId, before, after, csrf, offset,
        );
      }
      if (generation.current !== requestId) return;
      if (result.supplier_id !== supplierId
        || result.before_catalog_version_id !== before.catalog_version_id
        || result.after_catalog_version_id !== after.catalog_version_id
        || !result.requires_manager_review || result.offset !== offset
        || result.changes.length > 50) {
        throw new Error('Не удалось подтвердить результат сравнения. Выберите версии и повторите попытку.');
      }
      setReport(current => offset > 0 && current ? {
        ...result,
        changes: [...current.changes, ...result.changes],
      } : result);
    } catch (cause) {
      if (generation.current === requestId) setError(
        cause instanceof Error ? cause.message : 'Сравнение версий недоступно.',
      );
    } finally {
      if (generation.current === requestId) setBusy(false);
    }
  };
  return <details>
    <summary>{auditOnly ? 'Сравнение архивных прайсов' : 'Изменения товаров и цен'} · {supplierId}</summary>
    <p>Сравниваются два независимо подтверждённых источника. Порядок задаёт
      менеджер: базовая и сравниваемая версии, без предположений о дате.
      Сравнение не переносит результаты проверки и не обновляет КП автоматически.
      {auditOnly && ' Архивные версии доступны только для сравнения; '
        + 'они не добавляются к источникам новых КП.'}</p>
    <label>Базовая версия · {supplierId}
      <select value={baseline} disabled={busy} onChange={event => {
        reset(); setBaseline(event.target.value);
      }}>
        <option value="">Выберите базовый источник</option>
        {options.map(item => <option key={key(item.ref)} value={key(item.ref)}>
          {item.label}
        </option>)}
      </select>
    </label>
    <label>Сравниваемая версия · {supplierId}
      <select value={target} disabled={busy} onChange={event => {
        reset(); setTarget(event.target.value);
      }}>
        <option value="">Выберите сравниваемый источник</option>
        {options.map(item => <option key={key(item.ref)} value={key(item.ref)}>
          {item.label}
        </option>)}
      </select>
    </label>
    <button type="button" className="button button-white"
      disabled={busy || !baseline || !target || baseline === target
        || options.find(item => key(item.ref) === baseline)?.ref.catalog_version_id
          === options.find(item => key(item.ref) === target)?.ref.catalog_version_id}
      onClick={() => void request(0)}>Сравнить версии</button>
    {error && <p role="alert">{error}</p>}
    {report && <div role="group" aria-label={'Изменения прайса ' + supplierId}>
      <p>Источник A SHA-256: <code>{report.before_source_sha256}</code>.
        Источник B SHA-256: <code>{report.after_source_sha256}</code>.</p>
      <p>Изменённых товаров: <strong>{report.total_changes}</strong>;
        добавлено {report.counts.added}, удалено {report.counts.removed},
        только цена {report.counts.price_changed},
        только характеристики {report.counts.product_changed},
        цена и характеристики {report.counts.product_and_price_changed}.</p>
      {report.changes.length > 0 && <div className="supplier-inspection-table">
        <table>
          <thead><tr><th scope="col">Артикул</th><th scope="col">Изменение</th>
            <th scope="col">Было, ₽</th><th scope="col">Стало, ₽</th>
            <th scope="col">Разница, ₽</th></tr></thead>
          <tbody>{report.changes.map(item => <tr key={item.sku}>
            <th scope="row">{item.sku}</th>
            <td>{LABELS[item.kind]}{item.changed_fields.length > 0
              ? ' (' + item.changed_fields.join(', ') + ')' : ''}</td>
            <td>{item.before_price_rub ?? '—'}</td>
            <td>{item.after_price_rub ?? '—'}</td>
            <td>{item.comparable_price_delta_rub ?? 'Нельзя сравнить'}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      {report.total_changes === 0 && <p>Товары, цены и характеристики не изменились.</p>}
      {report.next_offset !== null && <button type="button"
        className="button button-white" disabled={busy}
        onClick={() => void request(report.next_offset!)}>
        Показать следующие изменения
      </button>}
      <p role="status">Отчёт только для анализа. Новую цену нельзя использовать
        вместо старой без повторной проверки источника и выбора товара менеджером.
        Если упаковка или характеристики изменились, разница цен не рассчитывается: товары нельзя сравнивать напрямую.</p>
    </div>}
  </details>;
}
