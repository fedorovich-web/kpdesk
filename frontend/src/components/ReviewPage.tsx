import { ArrowLeft, ArrowRight, BookOpen, ClipboardCheck, SearchCheck } from 'lucide-react';
import type { AutoDraft, ExtractedRequest } from '../domain';
import { money, readableReason } from '../domain';
import { designAssets } from '../design-assets';

export function ReviewPage({ draft, extracted, onBack, onPreview, isDemo = false }: {
  isDemo?: boolean;
  draft: AutoDraft | null;
  extracted: ExtractedRequest | null;
  onBack: () => void;
  onPreview?: () => void;
}) {
  if (!draft) return <section className="panel empty-state">
    <ClipboardCheck size={36} aria-hidden="true"/>
    <h2>Черновик ещё не сформирован</h2>
    <p>{isDemo ? 'Откройте учебную заявку и подберите товары из примера. Расчёт выполняется в этой вкладке.' : 'Добавьте заявку клиента, проверьте позиции и запустите подбор по прайсу.'}</p>
    <button className="button button-primary" type="button" onClick={onBack}>
      Новая заявка <ArrowRight size={17} aria-hidden="true"/>
    </button>
  </section>;

  const selected = draft.lines.filter(line => line.status === 'auto_selected').length;
  const needsReview = draft.lines.filter(line => line.status === 'needs_review').length;
  const noMatch = draft.lines.filter(line => line.status === 'no_match').length;
  const canShowTotal = draft.ready_for_draft && needsReview === 0 && noMatch === 0;
  const hasPricedTotal = draft.total_rub !== null && needsReview === 0 && noMatch === 0;

  return <>
    <div className="metric-grid review-stat-grid" role="group" aria-label="Анализ результатов подбора">
      <article className="metric-card"><span className="metric-icon"><img src={designAssets.icon.success} alt="" width={48} height={48}/></span>
        <div><span>Подобрано автоматически</span><strong>{selected}</strong><small>Соответствуют проверенным требованиям</small></div>
      </article>
      <article className="metric-card"><span className="metric-icon"><img src={designAssets.icon.checkQuote} alt="" width={48} height={48}/></span>
        <div><span>Требуют проверки</span><strong>{needsReview}</strong><small>Нужно уточнить характеристики</small></div>
      </article>
      <article className="metric-card"><span className="metric-icon"><SearchCheck size={24} aria-hidden="true"/></span>
        <div><span>Не найдены</span><strong>{noMatch}</strong><small>Не удалось подобрать</small></div>
      </article>
    </div>
    <div className="review-grid">
      <section className="panel" data-demo-target={isDemo ? 'results' : undefined}>
        <div className="panel-heading"><h2>Результаты подбора</h2>
          <p>{isDemo ? 'Проверьте выбранные товары, количество и суммы по учебному прайсу.' : 'Проверьте найденные товары, характеристики и суммы. Расчёт выполнен по загруженному прайсу.'}</p></div>
        <div className="table-scroll" role="region" tabIndex={0} aria-label="Прокручиваемые результаты подбора">
          <table className="data-table review-table">
            <caption className="sr-only">Построчный результат подбора и причины проверки</caption>
            <thead><tr><th scope="col">№</th><th scope="col">Позиция из заявки</th>
              <th scope="col">Результат</th><th scope="col">Сумма</th></tr></thead>
            <tbody>{draft.lines.map(line => {
              const source = extracted?.lines[line.line_number - 1];
              return <tr key={line.line_number}>
                <td>{line.line_number}</td>
                <td><strong>{source?.description ?? 'Позиция ' + line.line_number}</strong>
                  {line.sku && <small>артикул: {line.sku}</small>}</td>
                <td><span className={'status ' + line.status}>
                  {line.status === 'auto_selected' ? 'Подобрано' :
                    line.status === 'no_match' ? 'Не найдено' : 'Требует проверки'}
                </span>
                  {line.reason_codes.map(code => <small key={code} className="reason">{readableReason(code)}</small>)}
                </td>
                <td className="price-cell">{money(line.line_total_rub)}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        {draft.budget_exceeded && <div className="review-attention" role="alert">
          <h3>Превышен максимальный бюджет</h3>
          <p>Расчётная сумма КП {money(draft.total_rub)} превышает предел
            {' '}{money(draft.budget_max_rub)}. Для подготовки КП измените условия
            и подтвердите правила заново.</p>
        </div>}
        {(needsReview > 0 || noMatch > 0) &&
          <div className="review-attention" role="status" data-demo-target={isDemo ? 'exceptions' : undefined}>
            <h3>Есть позиции, которые нужно уточнить</h3>
            <p>Уточните характеристики в заявке и повторите подбор. Товары, которые не соответствуют обязательным требованиям, не будут выбраны.</p>
          </div>}
        <div className="panel-footer">
          <button className="button button-white" type="button" onClick={onBack}>
            <ArrowLeft size={17} aria-hidden="true"/> Уточнить заявку
          </button>
          {onPreview && <button className="button button-primary" type="button" onClick={onPreview}>
            Предпросмотр КП <ArrowRight size={17} aria-hidden="true"/>
          </button>}
        </div>
      </section>
      <aside className="request-side">
        <section className="panel side-info total-panel" data-demo-target={isDemo ? 'total' : undefined}>
          <h2>Итог черновика</h2>
          <dl>
            <div><dt>Всего позиций</dt><dd>{draft.lines.length}</dd></div>
            <div><dt>Подобрано</dt><dd>{selected}</dd></div>
            <div><dt>На проверке</dt><dd>{needsReview}</dd></div>
            <div><dt>Не найдено</dt><dd>{noMatch}</dd></div>
          </dl>
          {draft.base_total_rub !== undefined && draft.base_total_rub !== null &&
            <dl>
              <div><dt>Сумма по прайсу</dt><dd>{money(draft.base_total_rub)}</dd></div>
              <div><dt>Наценка</dt><dd>{draft.markup_percent ?? '0'}%</dd></div>
              {draft.budget_max_rub && <div><dt>Макс. бюджет</dt>
                <dd>{money(draft.budget_max_rub)}</dd></div>}
            </dl>}
          <div className="total-amount">
            <span>Сумма КП</span><strong>{hasPricedTotal ? money(draft.total_rub) : '—'}</strong>
          </div>
          <p className="total-note">{canShowTotal
            ? 'Стоимость рассчитана. Предложение ещё не утверждено и не отправлено клиенту.'
            : draft.budget_exceeded
              ? 'Сумма рассчитана, но превышает подтверждённый бюджет.'
              : 'Итоговая сумма появится после проверки спорных позиций.'}</p>
        </section>
        <section className="panel side-tip">
          <h2><BookOpen size={19} aria-hidden="true"/> Статус документа</h2>
          <p>{isDemo ? 'Учебный результат хранится только в этой вкладке. Можно распечатать демо; сохранение версий, утверждение и отправка здесь недоступны.' : 'Это предварительный результат. Чтобы сохранять версии, утверждать КП и скачивать PDF, войдите в личный кабинет.'}</p>
        </section>
      </aside>
    </div>
  </>;
}
