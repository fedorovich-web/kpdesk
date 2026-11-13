import type { SupplierStagePreview, SupplierStageRow } from '../tenant-session';
import { readableSupplierIssue } from '../supplier-issues';

type Props = {
  preview: SupplierStagePreview;
  busy: boolean;
  canPage: boolean;
  onPage: (offset: number) => void;
  onSelect: (row: SupplierStageRow) => void;
};

export function SupplierStageRows({ preview, busy, canPage, onPage, onSelect }: Props) {
  return <div className="supplier-preview" role="group" aria-label="Строки прайс-листа">
    <div className="supplier-preview-head">
      <strong>Всего строк: {preview.total_rows}</strong>
      <span>Требуют проверки: {preview.unresolved_rows}</span>
    </div>
    <details><summary>Сведения о файле</summary>
      <p className="access-hint">SHA-256: <code>{preview.source_sha256}</code></p>
      <p className="access-hint">
        Упаковки: столбец не выбран — {preview.pack_size_issue_counts.pack_size_requires_review ?? 0},
        ячейка пустая — {preview.pack_size_issue_counts.missing_pack_size ?? 0},
        значение некорректно — {preview.pack_size_issue_counts.invalid_pack_size ?? 0}.
      </p>
    </details>
    <ul className="supplier-cases">
      {preview.sampled_rows.map(row => <li key={row.line}>
        <div className="supplier-case">
          <span><strong>Строка {row.line} — {row.name || 'Без наименования'}</strong>
            <small>{row.supplier_sku || 'Без артикула'}
              {row.source_sale_unit ? ' · ' + row.source_sale_unit : ''}
              {row.source_pack_size !== null ? ' · фасовка ' + row.source_pack_size : ''}
            </small>
          </span>
          <span className="supplier-case-price">{row.price_rub ?? 'Цена не подтверждена'}</span>
        </div>
        <button type="button" className="button button-white" disabled={busy}
          onClick={() => onSelect(row)}>
          Проверить единицу цены
        </button>
        {row.pack_size_review_reason && <p className="supplier-risk">
          {row.pack_size_review_reason}
        </p>}
        {row.issues.length > 0 && <>
          <ul className="supplier-inspection-issues">{row.issues
            .filter(issue => !row.pack_size_review_reason
              || !['pack_size_requires_review', 'missing_pack_size', 'invalid_pack_size'].includes(issue))
            .map(issue => <li key={issue}>{readableSupplierIssue(issue)}</li>)}</ul>
          <details><summary>Технические сведения о замечаниях</summary>
            <p className="access-hint">SHA-256 строки: <code>{row.source_row_sha256}</code></p>
            <small className="access-hint">{row.issues.join(', ')}</small>
          </details>
        </>}
      </li>)}
    </ul>
    <div className="supplier-pagination">
      <button type="button" className="button button-white"
        disabled={busy || !canPage || preview.offset === 0}
        onClick={() => onPage(Math.max(0, preview.offset - 50))}>
        Предыдущие строки
      </button>
      <span>Позиции {preview.sampled_rows.length ? preview.offset + 1 : 0}–{
        preview.offset + preview.sampled_rows.length
      } из {preview.total_rows}</span>
      <button type="button" className="button button-white"
        disabled={busy || !canPage || preview.next_offset === null}
        onClick={() => {
          if (preview.next_offset !== null) onPage(preview.next_offset);
        }}>
        Следующие строки
      </button>
    </div>
  </div>;
}
