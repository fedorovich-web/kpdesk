import { ArrowRight, ClipboardCheck, FileSpreadsheet, FileText, History, ShieldCheck } from 'lucide-react';
import { designAssets } from '../design-assets';
import type { AutoDraft, Catalog } from '../domain';
import type { Page } from '../navigation';

type Destination = Extract<Page, 'request' | 'review' | 'import' | 'prices' | 'preview'>;

interface Props {
  isDemo?: boolean;
  catalog: Catalog | null;
  draft: AutoDraft | null;
  connectionError: string | null;
  onNavigate: (page: Destination) => void;
}

export function Dashboard({ catalog, draft, connectionError, onNavigate, isDemo = false }: Props) {
  const unresolved = draft?.lines.filter(line => line.status !== 'auto_selected').length ?? 0;
  const canPreview = draft !== null;
  return <>
    <section className="welcome" aria-labelledby="dashboard-title">
      <div className="welcome-copy">
        <h1 id="dashboard-title">Подготовьте предложение клиенту</h1>
        <p>{isDemo ? 'Познакомьтесь с подготовкой КП: откройте учебный прайс, проверьте заявку и посмотрите документ.' : 'Загрузите прайс, добавьте заявку клиента и проверьте товары для коммерческого предложения.'}</p>
        <p>{isDemo ? 'Учебная работа хранится в этой вкладке и сбрасывается после обновления страницы.' : 'Продолжите работу с текущим прайсом и черновиком. Сохранённые КП можно открыть в личном кабинете.'}</p>
      </div>
      <img className="dashboard-hero-image"
        src={designAssets.dashboardHero} alt="" aria-hidden="true"
        width={1448} height={1086} decoding="async" />
      <div className="welcome-actions">
        <button className="button button-primary" type="button" onClick={() => onNavigate('request')}>
          Создать КП <ArrowRight size={18} aria-hidden="true"/>
        </button>
        <button className="button button-white" type="button" onClick={() => onNavigate('import')}>
          <FileSpreadsheet size={18} aria-hidden="true"/> Загрузить прайс
        </button>
      </div>
    </section>

    <h2 className="section-heading">Подготовка КП</h2>
    <div className="quick-actions">
      <button className="quick-card" type="button" onClick={() => onNavigate('import')}>
        <span className="quick-icon"><img src={designAssets.icon.upload} alt="" width={48} height={48}/></span>
        <span><strong>1. Загрузить прайс</strong><small>{isDemo ? 'Учебный прайс · 5 вымышленных товаров' : 'Товары, цены и характеристики из Excel или CSV'}</small></span>
        <ArrowRight size={18} aria-hidden="true"/>
      </button>
      <button className="quick-card" type="button" onClick={() => onNavigate('request')}>
        <span className="quick-icon"><img src={designAssets.icon.createQuote} alt="" width={48} height={48}/></span>
        <span><strong>2. Создать КП</strong><small>{isDemo ? 'Учебный пример без обращения к ИИ' : 'Добавить товары вручную или распознать заявку'}</small></span>
        <ArrowRight size={18} aria-hidden="true"/>
      </button>
      {canPreview && <button className="quick-card" type="button" onClick={() => onNavigate('review')}>
        <span className="quick-icon"><img src={designAssets.icon.checkQuote} alt="" width={48} height={48}/></span>
        <span><strong>3. Проверить товары</strong><small>Проверить характеристики, количество и стоимость</small></span>
        <ArrowRight size={18} aria-hidden="true"/>
      </button>}
      {canPreview && <button className="quick-card" type="button" onClick={() => onNavigate('preview')}>
        <span className="quick-icon"><FileText size={25} aria-hidden="true"/></span>
        <span><strong>4. Посмотреть КП</strong><small>Проверить документ и распечатать черновик</small></span>
        <ArrowRight size={18} aria-hidden="true"/>
      </button>}
    </div>

    <div className="dashboard-section-heading">
      <h2 className="section-heading">Текущая работа</h2>
    </div>
    <div className="metric-grid">
      <div className="metric-card"><FileSpreadsheet size={28} aria-hidden="true"/>
        <div><span>Каталог</span><strong>{catalog ? 'Загружен' : 'Не загружен'}</strong></div>
      </div>
      <div className="metric-card"><FileSpreadsheet size={28} aria-hidden="true"/>
        <div><span>Товаров в прайсе</span><strong>{catalog?.rows_count ?? '—'}</strong></div>
      </div>
      <div className="metric-card"><ClipboardCheck size={28} aria-hidden="true"/>
        <div><span>Позиции в черновике</span><strong>{draft?.lines.length ?? '—'}</strong></div>
      </div>
      <div className="metric-card"><ShieldCheck size={28} aria-hidden="true"/>
        <div><span>Требуют проверки</span><strong>{draft ? unresolved : '—'}</strong></div>
      </div>
    </div>

    <div className="dashboard-bottom">
      <section className="panel dashboard-main-panel">
        <div className="panel-heading panel-heading-actions">
          <h2>Продолжить работу</h2>
          {catalog && <button className="text-button" type="button" onClick={() => onNavigate('prices')}>
            Открыть каталог <ArrowRight size={17} aria-hidden="true"/>
          </button>}
        </div>
        <div className="empty-state">
          {connectionError
            ? <><h3>Не удалось загрузить данные</h3><p>{connectionError}</p></>
            : !catalog
              ? <><FileSpreadsheet size={32} aria-hidden="true"/><h3>Начните с прайс-листа</h3>
                  <p>{isDemo ? 'Активируйте учебный прайс или загрузите точную копию CSV. Проверка и расчёт выполняются в браузере.' : 'Загрузите Excel или CSV по шаблону. После проверки товары и цены станут доступны для подбора.'}</p></>
              : draft
                ? <><ClipboardCheck size={32} aria-hidden="true"/><h3>Черновик рассчитан</h3>
                    <p>{unresolved
                      ? `Позиции, требующие проверки: ${unresolved}. Уточните их перед подготовкой документа.`
                      : isDemo ? 'Учебные позиции сопоставлены. Доступна печать демо; утверждение и отправка здесь не выполняются.' : 'Товары подобраны. Проверьте их и утвердите предложение в личном кабинете перед отправкой клиенту.'}</p></>
                : <><FileText size={32} aria-hidden="true"/><h3>Прайс готов к подбору</h3>
                    <p>Добавьте заявку, проверьте товары и рассчитайте стоимость предложения.</p></>}
        </div>
      </section>
      <section className="panel guide-panel">
        <h2>Справка и материалы</h2>
        <div className="dashboard-help-links">
          <a className="dashboard-help-link" href="/templates/kpdesk-portfolio-synthetic-v1.csv" download>
            <FileSpreadsheet size={28} aria-hidden="true"/>
            <span><strong>Учебный прайс CSV</strong><small>Пять вымышленных товаров для самостоятельной проверки</small></span>
            <ArrowRight size={16} aria-hidden="true"/>
          </a>
          {!isDemo && <a className="dashboard-help-link" href="/templates/kpdesk-catalog-v2-template.xlsx" download>
            <FileSpreadsheet size={28} aria-hidden="true"/>
            <span><strong>Шаблон Excel</strong><small>Формат каталога для импорта</small></span>
            <ArrowRight size={16} aria-hidden="true"/>
          </a>}
          <a className="dashboard-help-link" href="/faq">
            <FileText size={28} aria-hidden="true"/>
            <span><strong>Ответы на вопросы</strong><small>Как загрузить прайс и подготовить КП</small></span>
            <ArrowRight size={16} aria-hidden="true"/>
          </a>
          <a className="dashboard-help-link" href="/app">
            <History size={28} aria-hidden="true"/>
            <span><strong>Личный кабинет</strong><small>Прайсы компании и сохранённые предложения</small></span>
            <ArrowRight size={16} aria-hidden="true"/>
          </a>
        </div>
      </section>
    </div>
  </>;
}
