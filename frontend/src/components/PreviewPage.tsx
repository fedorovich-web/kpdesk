import { useState } from 'react';
import { ArrowLeft, FileText, Printer, ShieldAlert } from 'lucide-react';
import type { AutoDraft, ExtractedRequest } from '../domain';
import { money, readableReason } from '../domain';
import { designAssets } from '../design-assets';
import { previewDemo } from '../demo/preview';
import { demoProposalExample } from '../demo/narrative';

type Template = 'classic' | 'modern' | 'minimal';
type Scale = '75' | '100' | '125';
const templates: { id: Template; label: string }[] = [
  { id: 'classic', label: 'Классический' },
  { id: 'modern', label: 'Современный' },
  { id: 'minimal', label: 'Минималистичный' },
];

export function PreviewPage({ draft, extracted, onBack, onNewRequest, isDemo = false }: {
  isDemo?: boolean;
  draft: AutoDraft | null;
  extracted: ExtractedRequest | null;
  onBack: () => void;
  onNewRequest: () => void;
}) {
  const [template, setTemplate] = useState<Template>('classic');
  const [zoom, setZoom] = useState<Scale>('100');
  const [showLogo, setShowLogo] = useState(true);
  const [showReasons, setShowReasons] = useState(true);
  const [showDemo, setShowDemo] = useState(false);
  const isSpecimen = !draft || !extracted;
  const demoMode = isDemo || isSpecimen;

  if (isSpecimen && !showDemo) return <div className="preview-empty panel empty-state">
    <FileText size={36} aria-hidden="true"/>
    <h1>Нет черновика для предпросмотра</h1>
    <p>Добавьте заявку и подберите товары из прайса. Здесь появится черновик для проверки и печати.</p>
    <div className="preview-empty-actions">
      <button type="button" className="button button-primary" onClick={onNewRequest}>Создать черновик</button>
      <button type="button" className="button button-white" onClick={() => setShowDemo(true)}>
        Посмотреть пример оформления
      </button>
    </div>
  </div>;

  const preview = draft && extracted ? { draft, extracted } : previewDemo;
  const currentDraft = preview.draft;
  const currentExtracted = preview.extracted;
  const unresolved = currentDraft.lines.some(line => line.status !== 'auto_selected');
  const showTotal = currentDraft.ready_for_draft && !unresolved && currentDraft.total_rub !== null;
  const printAllowed = showTotal;
  const example = isDemo && !isSpecimen ? demoProposalExample(currentDraft, currentExtracted) : null;

  return <div className="preview-page">
    <div className="preview-heading heading-row">
      <div><h1>Предпросмотр КП</h1>
        <p>{isSpecimen ? 'Пример оформления с вымышленными товарами и ценами. Это демонстрация, без расчёта по вашему прайсу.'
          : isDemo ? 'Учебный документ с вымышленными товарами и ценами. Он хранится только в этой вкладке.'
            : 'Проверьте документ перед печатью. Это неутверждённый черновик, не сохранённый на сервере.'}</p></div>
      <div className="heading-actions">
        <button className="button button-white" type="button" onClick={onBack}>
          <ArrowLeft size={17} aria-hidden="true"/> К проверке</button>
        <button className="button button-primary" type="button" disabled={!printAllowed}
          onClick={() => window.print()}>
          <Printer size={18} aria-hidden="true"/> Печать / сохранить PDF</button>
      </div>
    </div>
    <div className="preview-workspace">
      <section className="preview-viewer" aria-label="Предпросмотр документа" data-demo-target={isDemo ? 'preview' : undefined}>
        <div className="preview-toolbar">
          <span><FileText size={18} aria-hidden="true"/> Черновик КП</span>
          <label>Масштаб
            <select value={zoom} onChange={event => setZoom(event.target.value as Scale)}>
              <option value="75">75%</option><option value="100">100%</option><option value="125">125%</option>
            </select>
          </label>
        </div>
        <div className={'preview-paper-stage preview-zoom-' + zoom}>
          <article className={'print-sheet template-' + template} aria-label="Неутверждённое коммерческое предложение">
            <header className="print-header">
              {showLogo && <img src={designAssets.logo} alt="KPDesk" width={203} height={52}/>}
              <span className="print-draft-mark">{demoMode ? 'ДЕМО — НЕ ЯВЛЯЕТСЯ КП' : 'ЧЕРНОВИК — НЕ УТВЕРЖДЕНО'}</span>
            </header>
            <h2>Коммерческое предложение</h2>
            <p className="print-lead">{isSpecimen ? 'Пример таблицы товаров с вымышленными данными. Расчёт по вашему прайсу не выполнялся.'
              : isDemo ? 'Учебный документ с вымышленными товарами, ценами и примером расчёта.'
                : 'Товары и стоимость по прайсу на основании заявки клиента.'}</p>
            <div className="print-details">
              <p><strong>Статус:</strong> {demoMode ? 'Демонстрационные данные' : unresolved ? 'Требуется проверка' : 'Автоматически рассчитано'}</p>
              <p><strong>Позиции:</strong> {currentDraft.lines.length}</p>
              {!isSpecimen && currentDraft.base_total_rub && <p>
                <strong>Сумма по прайсу:</strong> {money(currentDraft.base_total_rub)};
                {' '}<strong>наценка:</strong> {currentDraft.markup_percent ?? '0'}%
              </p>}
              {!isSpecimen && currentDraft.budget_max_rub && <p>
                <strong>Максимальный бюджет:</strong> {money(currentDraft.budget_max_rub)}
              </p>}
              <p><strong>Версия каталога:</strong> <span className="print-version">{currentDraft.catalog_version_id}</span></p>
            </div>
            {example && <section className="print-narrative" aria-label="Учебный текст предложения">
              <h3>Товары для вашего заказа</h3>
              <p>Пример текста для выбранных товаров из учебного прайса.</p>
              <p>{example.composition}</p>
              <p>{example.purpose}</p>
              <h3>Следующий шаг</h3>
              <p>{example.nextStep}</p>
            </section>}
            <div className="print-table-scroll">
              <table className="print-quote-table">
                <caption className="sr-only">Позиции из расчёта KPDesk</caption>
                <thead><tr>
                  <th scope="col">№</th><th scope="col">Наименование</th>
                  <th scope="col">Артикул</th><th scope="col">Количество</th>
                  <th scope="col">Статус</th><th scope="col">Сумма, ₽</th>
                </tr></thead>
                <tbody>{currentDraft.lines.map(line => {
                  const source = currentExtracted.lines[line.line_number - 1];
                  return <tr key={line.line_number}>
                    <td>{line.line_number}</td>
                    <td>{source?.description || 'Позиция ' + line.line_number}
                      {showReasons && line.reason_codes.length > 0 &&
                        <small className="print-reasons">{line.reason_codes.map(readableReason).join('; ')}</small>}
                    </td>
                    <td>{line.sku ?? '—'}</td>
                    <td>{source?.quantity != null ? source.quantity : '—'} {source?.unit ?? ''}</td>
                    <td>{line.status === 'auto_selected' ? 'Подобрано' :
                      line.status === 'no_match' ? 'Не найдено' : 'На проверке'}</td>
                    <td>{money(line.line_total_rub)}</td>
                  </tr>;
                })}</tbody>
              </table>
            </div>
            <div className="print-summary">
              <span>Сумма КП</span><strong>{!unresolved ? money(currentDraft.total_rub) : '—'}</strong>
            </div>
            {!showTotal && <p className="print-warning">
              {currentDraft.budget_exceeded
                ? 'Превышен подтверждённый бюджет. Печать КП заблокирована.'
                : 'Есть позиции, требующие проверки. Печать КП заблокирована.'}
            </p>}
            <footer className="print-footer">
              <p>{demoMode ? 'Демонстрационные данные. Не является счётом, офертой или действующим коммерческим предложением.' : 'Документ сформирован для предварительной проверки. КП не утверждено и не отправлено клиенту.'}</p>
              <p>Данные заказчика и поставщика, НДС и сроки поставки не указаны: их нужно подтвердить отдельно.</p>
            </footer>
          </article>
        </div>
      </section>

      <aside className="preview-controls">
        <section className="panel">
          <h2>Вид документа</h2>
          <p>Настройте вид черновика перед печатью. Товары и суммы останутся прежними.</p>
          <label className="preview-toggle">
            <span>Показывать логотип</span>
            <input type="checkbox" checked={showLogo} onChange={event => setShowLogo(event.target.checked)}/>
          </label>
          <label className="preview-toggle">
            <span>Причины проверки позиций</span>
            <input type="checkbox" checked={showReasons} onChange={event => setShowReasons(event.target.checked)}/>
          </label>
        </section>
        <section className="panel">
          <h2>Шаблон документа</h2>
          <div className="preview-templates" role="group" aria-label="Вид документа" data-demo-target={isDemo ? 'templates' : undefined}>
            {templates.map(option => <button key={option.id} type="button"
              aria-pressed={template === option.id}
              className={'preview-template' + (template === option.id ? ' selected' : '')}
              onClick={() => setTemplate(option.id)}>
              <span className={'preview-template-example preview-template-' + option.id} aria-hidden="true">
                <span/><span/><span/>
              </span>
              {option.label}
            </button>)}
          </div>
        </section>
        <section className="panel preview-print-actions">
          <h2>Сохранение документа</h2>
          <button type="button" className="button button-primary button-full" data-demo-target={isDemo ? 'print' : undefined}
            disabled={!printAllowed}
            onClick={() => window.print()}><Printer size={17} aria-hidden="true"/> Печать / сохранить PDF</button>
          <p>Сохранение в PDF доступно через системное окно печати браузера.</p>
          <div className="preview-safety"><ShieldAlert size={19} aria-hidden="true"/>
            <span>Чтобы сохранить версию КП и утвердить её перед отправкой, войдите в личный кабинет.</span>
          </div>
        </section>
      </aside>
    </div>
  </div>;
}
