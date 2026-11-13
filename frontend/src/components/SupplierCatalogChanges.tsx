import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type {
  DiscoveredSupplierSource, HistoricalSupplierSource,
  HistoricalSupplierSourceRef, SupplierSourceRef,
} from '../tenant-session';
import { SupplierCatalogVersionDiff } from './SupplierCatalogVersionDiff';

type Props = { organizationId: string; csrf: string };
const supplierPattern = /^[A-Za-z0-9_.-]{1,120}$/;

export function SupplierCatalogChanges({
  organizationId, csrf,
}: Props) {
  const [supplierId, setSupplierId] = useState('');
  const [versions, setVersions] = useState<DiscoveredSupplierSource[]>([]);
  const [historical, setHistorical] = useState<HistoricalSupplierSource[]>([]);
  const [mode, setMode] = useState<'current' | 'history'>('current');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const epoch = useRef(0);

  useEffect(() => {
    epoch.current++;
    setSupplierId('');
    setVersions([]);
    setHistorical([]);
    setNotice('');
    setBusy(false);
    return () => { epoch.current++; };
  }, [organizationId]);

  const changeSupplier = (supplier: string) => {
    epoch.current++;
    setSupplierId(supplier);
    setVersions([]);
    setHistorical([]);
    setNotice('');
    setBusy(false);
  };

  const loadVersions = async () => {
    if (busy || !supplierPattern.test(supplierId)) {
      setNotice('Укажите ID группы ранее подтверждённых прайсов.');
      return;
    }
    const current = ++epoch.current;
    setBusy(true);
    setVersions([]);
    setHistorical([]);
    setNotice('');
    try {
      if (mode === 'history') {
        const listing = await sessionApi.supplierHistoricalSources(
          organizationId, supplierId, csrf,
        );
        if (current !== epoch.current) return;
        if (listing.supplier_id !== supplierId || !listing.audit_only
          || listing.options.some(source => source.supplier_id !== supplierId)) {
          throw new Error('История содержит источники другого поставщика.');
        }
        setHistorical(listing.options);
        if (listing.truncated) setNotice(
          'Архив ограничен первыми 500 подтверждениями и 200 источниками.',
        );
        else if (new Set(listing.options.map(item => item.catalog_version_id)).size < 2)
          setNotice('Для сравнения нужны два ранее загруженных исходных прайса.');
        return;
      }
      const group = await sessionApi.supplierVersions(organizationId, supplierId, csrf);
      if (current !== epoch.current) return;
      if (group.supplier_id !== supplierId ||
        group.options.some(source => source.supplier_id !== supplierId)) {
        throw new Error('Ответ содержит версии другого поставщика.');
      }
      setVersions(group.options);
      if (group.truncated) {
        setNotice('Показана часть истории. Более ранние версии могут отсутствовать в списке.');
      } else if (!group.canonical_discovery_available) {
        setNotice('Подготовленные прайсы пока недоступны.');
      } else if (new Set(group.options.map(item => item.catalog_version_id)).size < 2) {
        setNotice('Для сравнения нужны две независимо проверенные версии.');
      }
    } catch (cause) {
      if (current === epoch.current) setNotice(
        cause instanceof Error ? cause.message : 'Не удалось загрузить версии.',
      );
    } finally {
      if (current === epoch.current) setBusy(false);
    }
  };

  const options = versions.map(item => ({
    ref: {
      supplier_id: item.supplier_id,
      profile_fingerprint: item.profile_fingerprint,
      catalog_version_id: item.catalog_version_id,
    } satisfies SupplierSourceRef,
    label: (item.evidence === 'reviewed_canonical'
      ? 'Подготовленный прайс' : 'Проверенный исходный прайс')
      + ' · версия ' + item.catalog_version_id.slice(0, 8)
      + ' · товаров: ' + item.rows_count + ' · источник ' + item.source_sha256.slice(0, 12),
  }));

  const historyOptions = historical.map(item => ({
    ref: {
      supplier_id: item.supplier_id,
      profile_sha256: item.profile_sha256,
      catalog_version_id: item.catalog_version_id,
    } satisfies HistoricalSupplierSourceRef,
    label: 'Архивный исходный прайс · версия ' + item.catalog_version_id.slice(0, 8)
      + ' · товаров: ' + item.rows_count + ' · источник ' + item.source_sha256.slice(0, 12),
  }));
  const changeMode = (next: 'current' | 'history') => {
    epoch.current++;
    setMode(next);
    setBusy(false);
    setVersions([]);
    setHistorical([]);
    setNotice('');
  };

  return <section className="access-catalog-state" aria-label="Изменения прайсов поставщиков">
    <h2>Изменения прайса поставщика</h2>
    <p>Сравнение доступно только для двух независимо подтверждённых версий.
      История не подтверждает цену автоматически; старые КП
      и решения менеджера не переписываются.</p>
    <label>Режим сравнения
      <select aria-label="Режим истории поставщика" value={mode} disabled={busy}
        onChange={event => changeMode(
          event.target.value as 'current' | 'history',
        )}>
        <option value="current">Актуальные подтверждённые версии для КП</option>
        <option value="history">Сравнение архивных прайсов (не для новых КП)</option>
      </select>
    </label>
    <label>Поставщик для сравнения
      <input aria-label="ID поставщика для сравнения" maxLength={120}
        disabled={busy} value={supplierId} placeholder="supplier-company"
        onChange={event => changeSupplier(event.target.value)}/>
    </label>
    <button type="button" className="button button-white"
      disabled={busy || !supplierPattern.test(supplierId)}
      onClick={() => void loadVersions()}>Найти подтверждённые версии</button>
    {notice && <p role="status">{notice}</p>}
    {(mode === 'history'
      ? new Set(historical.map(item => item.catalog_version_id)).size >= 2
      : new Set(versions.map(item => item.catalog_version_id)).size >= 2) &&
      <SupplierCatalogVersionDiff
        key={supplierId + mode + (mode === 'history'
          ? historical.map(item => item.profile_sha256).join('|')
          : versions.map(item => item.catalog_version_id).join('|'))}
        organizationId={organizationId} csrf={csrf} supplierId={supplierId}
        auditOnly={mode === 'history'}
        options={mode === 'history' ? historyOptions : options}/>}
  </section>;
}
