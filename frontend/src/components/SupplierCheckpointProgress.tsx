/** A89 opt-in checkpoint persistence for multi-page reviewed supplier files.
 * Deliberately absent from the production application until Alembic rollout.
 */
import { useEffect, useRef, useState } from 'react';
import { SessionApiError } from '../tenant-session';
import type { ActiveTenantCatalog } from '../tenant-session';
import { publishSavedSupplierPages } from '../supplier-large-publication';
import type {
  SupplierCanonicalDecision, SupplierStagePreview,
} from '../tenant-session';
import {
  appendSupplierReviewPage, restoreSupplierReviewPages,
} from '../supplier-paged-checkpoint';
import type { PagedReviewScope, SupplierSavedProgress } from '../supplier-paged-checkpoint';

type Props = PagedReviewScope & {
  stage: SupplierStagePreview;
  reviews: Record<number, SupplierCanonicalDecision>;
  onRestored: (decisions: Record<number, SupplierCanonicalDecision>) => void;
  busy: boolean;
  priceBasisConfirmed: boolean;
  onPublished?: (catalog: ActiveTenantCatalog) => void;
};

function newAppendKey(): string {
  if (!globalThis.crypto?.randomUUID) {
    throw new SessionApiError('secure_random_unavailable',
      'Не удалось создать защищённый идентификатор сохранения.', 0);
  }
  return 'review-page-' + crypto.randomUUID();
}

function canonicalDecision(value: SupplierCanonicalDecision): string {
  // Server JSONB/Pydantic can change object property order without changing
  // the operator's decision. Do not treat key ordering as an edit.
  return JSON.stringify(value, (_key: string, item: unknown) => {
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      return Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)));
    }
    return item;
  });
}

function sameDecision(a: SupplierCanonicalDecision, b: SupplierCanonicalDecision): boolean {
  return canonicalDecision(a) === canonicalDecision(b);
}

export function SupplierCheckpointProgress({
  stage, reviews, onRestored, busy, priceBasisConfirmed, onPublished, ...scope
}: Props) {
  const [saved, setSaved] = useState<SupplierSavedProgress | null>(null);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [allowDiscard, setAllowDiscard] = useState(false);
  const [publishedId, setPublishedId] = useState<string | null>(null);
  const mounted = useRef(true);
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const publishPending = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const visible = stage.sampled_rows;
  const changed = visible.some(row => {
    const existing = saved?.decisions[row.line];
    const draft = reviews[row.line];
    return existing && draft && !sameDecision(existing, draft);
  });
  const unsaved = visible.flatMap(row => {
    const decision = reviews[row.line];
    return decision && saved?.decisions[row.line] === undefined ? [decision] : [];
  });
  const dirtyAnywhere = Object.values(reviews).some(decision => {
    const recorded = saved?.decisions[decision.line];
    return !recorded || !sameDecision(recorded, decision);
  });
  const canSave = !working && !busy && priceBasisConfirmed && !changed
    && unsaved.length > 0 && unsaved.length <= 50
    && stage.total_rows <= 5000 && stage.total_rows > 50
    && (saved?.totalCount ?? 0) + unsaved.length <= stage.total_rows;

  const save = async () => {
    if (!canSave) return;
    const sorted = [...unsaved].sort((a, b) => a.line - b.line);
    const signature = JSON.stringify({
      org: scope.organizationId, supplier: scope.supplierId,
      source: scope.review.source_sha256, mapping: scope.review.mapping,
      basis: scope.review.price_basis, policy: scope.review.formula_policy,
      head: saved?.checkpointId ?? null, decisions: sorted,
    });
    setWorking(true); setError(''); setMessage(''); setPublishedId(null);
    try {
      const existing = pending.current;
      const key = existing?.signature === signature
        ? existing.key : newAppendKey();
      pending.current = { signature, key };
      const receipt = await appendSupplierReviewPage(
        scope, sorted, saved?.checkpointId ?? null, key,
      );
      if (!mounted.current) return;
      if (saved && (receipt.mappingSha256 !== saved.mappingSha256
        || receipt.checkpointId === saved.checkpointId)) {
        throw new SessionApiError('checkpoint_receipt_conflict',
          'Сервер не подтвердил новую версию сохранённых решений.', 409);
      }
      setSaved(previous => ({
        checkpointId: receipt.checkpointId, mappingSha256: receipt.mappingSha256,
        decisions: { ...(previous?.decisions ?? {}),
          ...Object.fromEntries(sorted.map(item => [item.line, item])) },
        totalCount: (previous?.totalCount ?? 0) + sorted.length,
      }));
      pending.current = null;
      setMessage('Результаты проверки строк сохранены.');
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Не удалось сохранить решения.');
      }
    } finally {
      if (mounted.current) setWorking(false);
    }
  };

  const restore = async () => {
    if (working || busy) return;
    if (dirtyAnywhere && !allowDiscard) {
      setError('Есть несохранённые решения. Для восстановления подтвердите их замену.');
      return;
    }
    setWorking(true); setError(''); setMessage('');
    try {
      const loaded = await restoreSupplierReviewPages(scope);
      if (!mounted.current) return;
      if (!loaded) {
        if (saved) {
          throw new SessionApiError('checkpoint_disappeared',
            'Ранее сохранённые решения больше недоступны.', 409);
        }
        setMessage('Для этого прайс-листа сохранённых решений пока нет.');
        return;
      }
      if (loaded.totalCount > stage.total_rows || visible.some(row => {
        const decision = loaded.decisions[row.line];
        return decision && decision.source_row_sha256 !== row.source_row_sha256;
      })) {
        throw new Error('Сохранённые решения относятся к другим строкам. Проверьте исходный файл.');
      }
      setSaved(loaded);
      setPublishedId(null);
      setAllowDiscard(false);
      pending.current = null;
      onRestored({ ...loaded.decisions });
      setMessage('Восстановлены подтверждённые решения: ' + loaded.totalCount + '.');
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Не удалось восстановить решения.');
      }
    } finally {
      if (mounted.current) setWorking(false);
    }
  };

  const complete = saved !== null
    && saved.totalCount === stage.total_rows
    && Object.keys(saved.decisions).length === stage.total_rows
    && stage.total_rows > 50 && stage.total_rows <= 5000
    && Object.values(saved.decisions).some(item => item.action === 'include');
  const canPublish = complete && !working && !busy && !dirtyAnywhere
    && !changed && priceBasisConfirmed;

  const publish = async () => {
    if (!canPublish || !saved) return;
    setWorking(true); setError(''); setMessage(''); setPublishedId(null);
    try {
      const signature = JSON.stringify({
        organizationId: scope.organizationId,
        supplierId: scope.supplierId, source: scope.review.source_sha256,
        mapping: scope.review.mapping, policy: scope.review.formula_policy,
        basis: scope.review.price_basis, checkpointId: saved.checkpointId,
      });
      const previous = publishPending.current;
      const key = previous?.signature === signature ? previous.key
        : 'review-large-' + crypto.randomUUID();
      publishPending.current = { signature, key };
      const result = await publishSavedSupplierPages(scope, saved, stage.total_rows, key);
      if (!mounted.current) return;
      setPublishedId(result.version_id);
      onPublished?.(result);
      setMessage('Каталог опубликован после повторной проверки всех сохранённых строк.');
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Публикация не выполнена.');
      }
    } finally {
      if (mounted.current) setWorking(false);
    }
  };

  return <section aria-label="Сохранение проверки больших прайс-листов">
    <h4>Сохранение проверки прайса</h4>
    <p className="access-hint">
      Сохранено {saved?.totalCount ?? 0} из {stage.total_rows} решений.
      За один запрос сохраняется не более 50 подтверждённых строк.
      Изолированный стенд: публикация доступна только после сохранения всех решений,
      повторной серверной проверки и подтверждения типа цены.
    </p>
    {changed && <p role="alert" className="alert error">
      Уже сохранённая строка была изменена. Для замены требуется отдельное подтверждение.
    </p>}
    <button type="button" className="button button-white"
      disabled={!canSave} onClick={() => void save()}>
      {working ? 'Обработка…' : 'Сохранить строки текущей страницы: ' + unsaved.length}
    </button>
    {dirtyAnywhere && <label className="tenant-checkbox">
      <input type="checkbox" checked={allowDiscard}
        disabled={working || busy} onChange={event => setAllowDiscard(event.target.checked)}/>
      Я понимаю, что восстановление заменит несохранённые локальные решения
    </label>}
    <button type="button" className="button button-white"
      disabled={working || busy || dirtyAnywhere && !allowDiscard}
      onClick={() => void restore()}>
      Восстановить сохранённые решения
    </button>
    <button type="button" className="button button-primary"
      disabled={!canPublish} onClick={() => void publish()}>
      {working ? 'Проверка…' : 'Проверить и опубликовать весь прайс (тестовый стенд)'}
    </button>
    {publishedId && <p role="status">Опубликована версия каталога: {publishedId}</p>}
    {error && <p role="alert" className="alert error">{error}</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
