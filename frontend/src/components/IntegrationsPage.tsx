import { useEffect } from 'react';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { designAssets } from '../design-assets';
import { integrationCards } from '../content/marketing';
import { applyDocumentSeo } from '../seo';
import { MarketingCta, MarketingHero, MarketingPage, MarketingSection } from './MarketingShared';

const tasks = [
  { title: 'Товары для автоматического подбора', text: 'Добавьте характеристики в прайс, чтобы сравнивать позиции с требованиями заявки.', icon: designAssets.icon.upload },
  { title: 'Цены для расчёта предложения', text: 'KPDesk использует цены выбранных товаров и количество из заявки для расчёта строк и итога.', icon: designAssets.icon.team },
  { title: 'Каталог для следующих заявок', text: 'Сохраните проверенный прайс и используйте его в новых предложениях без повторной загрузки.', icon: designAssets.icon.createQuote },
  { title: 'История с прежними ценами', text: 'Обновляйте каталог, когда меняются цены. Суммы уже сохранённых КП останутся прежними.', icon: designAssets.icon.database },
] as const;

export function IntegrationsPage() {
  useEffect(() => applyDocumentSeo('integrations'), []);
  return <MarketingPage>
    <MarketingHero
      title="Ваш прайс — основа подбора и расчёта КП"
      description="Загрузите товары, характеристики и цены из CSV или Excel (.xlsx). KPDesk будет предлагать позиции из этого каталога по заявкам клиентов и рассчитывать стоимость. Данные из 1С и CRM можно перенести через подготовленную выгрузку."
      image={designAssets.integrationsHero}
      imageAlt="Иллюстрация источников товарных данных для подготовки прайса"
      anchor="#integrations-list" secondaryLabel="Способы загрузки"
      highlights={['Excel и CSV', 'Выгрузки из 1С', 'Товары и цены в каталоге'] }
    />
    <MarketingSection id="integrations-list" heading="Перенесите товары из своей таблицы или выгрузки">
      <div className="marketing-integration-grid">
        {integrationCards.map(({ name, text, state, icon }) => <article key={name}>
          <img className="marketing-integration-icon" src={icon} alt="" width={48} height={48} loading="lazy"/>
          <h3>{name}</h3><p>{text}</p>
          <span className="marketing-status">{state}</span>
        </article>)}
      </div>
    </MarketingSection>
    <MarketingSection heading="Что прайс даёт при подготовке КП" modifier="marketing-task-section">
      <div className="marketing-task-grid">{tasks.map(({ title, text, icon }) =>
        <article key={title}><img src={icon} alt="" width={48} height={48} loading="lazy"/>
          <h3>{title}</h3><p>{text}</p></article>)}</div>
    </MarketingSection>
    <MarketingSection id="api" heading="Что должно быть в прайсе" modifier="marketing-api-section">
      <div className="marketing-api-grid">
        <div><p>Для расчёта нужны названия товаров, артикулы, единицы и цены. Добавьте характеристики для подбора по требованиям клиента. Приведите столбцы к формату KPDesk и проверьте файл перед загрузкой. Данные из 1С, Битрикс24 и amoCRM загружаются через подготовленную таблицу; прямая синхронизация с этими системами пока не подключена.</p>
          <a href="/faq" className="button button-primary">Вопросы о прайсах <ArrowRight size={17} aria-hidden="true"/></a></div>
        <img src={designAssets.integrationScheme} alt="Иллюстрация работы с товарными данными в KPDesk"
          width={1448} height={1086} loading="lazy"/>
        <ul className="marketing-api-checks">
          <li><CheckCircle2 aria-hidden="true"/>Импорт CSV и XLSX</li>
          <li><CheckCircle2 aria-hidden="true"/>Подготовка столбцов таблицы</li>
          <li><CheckCircle2 aria-hidden="true"/>Проверка товаров и цен</li>
          <li><CheckCircle2 aria-hidden="true"/>История обновлений прайса</li>
        </ul>
      </div>
    </MarketingSection>
    <MarketingCta title="Посмотрите, как работает подбор по прайсу"
      description="В демо уже загружен учебный каталог. Попробуйте найти товары для заявки и рассчитать предложение."/>
  </MarketingPage>;
}
