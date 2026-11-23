import { useEffect } from 'react';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { designAssets, publicIconSrcSet } from '../design-assets';
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
      title="Импорт прайсов Excel и CSV для расчёта КП"
      description="Импортируйте товары, цены и характеристики из CSV или Excel (.xlsx). Начните со своей таблицы или подготовьте выгрузку из 1С, CRM или Google Sheets."
      image={designAssets.integrationsHero}
      imageAlt="Иллюстрация источников товарных данных для подготовки прайса"
      anchor="#integrations-list" secondaryLabel="Способы загрузки"
      highlights={['Прайсы CSV и XLSX', 'Импорт выгрузок из 1С', 'Каталог для новых КП'] }
    />
    <MarketingSection id="integrations-list" heading="Откуда можно загрузить товары и цены">
      <div className="marketing-integration-grid">
        {integrationCards.map(({ name, text, state, icon }) => <article key={name}>
          <img className="marketing-integration-icon" src={icon}
            srcSet={icon.endsWith('.avif') ? publicIconSrcSet(icon) : undefined} sizes="48px"
            alt="" width={48} height={48} loading="eager" decoding="async" fetchPriority="low"/>
          <h3>{name}</h3><p>{text}</p>
          <span className="marketing-status">{state}</span>
        </article>)}
      </div>
    </MarketingSection>
    <MarketingSection heading="Как каталог используется в новых КП" modifier="marketing-task-section">
      <div className="marketing-task-grid">{tasks.map(({ title, text, icon }) =>
        <article key={title}><img src={icon} srcSet={publicIconSrcSet(icon)} sizes="48px" alt="" width={48} height={48} loading="eager" decoding="async" fetchPriority="low"/>
          <h3>{title}</h3><p>{text}</p></article>)}</div>
    </MarketingSection>
    <MarketingSection id="api" heading="Четыре обязательных столбца прайса" modifier="marketing-api-section">
      <div className="marketing-api-grid">
        <div><p>Добавьте в прайс артикул, название, цену в рублях и единицу продажи. В шаблоне это столбцы <code>sku</code>, <code>name</code>, <code>price_rub</code> и <code>sale_unit</code>. Характеристики можно вынести в отдельные столбцы.</p>
          <a href="/faq" className="button button-primary">Вопросы о прайсах <ArrowRight size={17} aria-hidden="true"/></a></div>
        <figure className="marketing-api-diagram">
          <img src={designAssets.integrationScheme} alt="Иллюстрация работы с товарными данными в KPDesk"
            width={1448} height={1086} loading="eager" decoding="async" fetchPriority="low"
            srcSet="/assets/hero/07_hero_integrations_scheme-640w.avif 640w, /assets/hero/07_hero_integrations_scheme-960w.avif 960w, /assets/hero/07_hero_integrations_scheme.avif 1448w"
            sizes="(max-width: 470px) calc(100vw - 72px), (max-width: 600px) 410px, (max-width: 900px) 46vw, (max-width: 1300px) 34vw, 410px"/>
        </figure>
        <ul className="marketing-api-checks">
          <li><CheckCircle2 aria-hidden="true"/>Импорт CSV и XLSX</li>
          <li><CheckCircle2 aria-hidden="true"/>Подготовка столбцов таблицы</li>
          <li><CheckCircle2 aria-hidden="true"/>Проверка товаров и цен</li>
          <li><CheckCircle2 aria-hidden="true"/>История обновлений прайса</li>
        </ul>
      </div>
    </MarketingSection>
    <MarketingCta title="Загрузите прайс для первого КП"
      description="Загрузите товары и цены, добавьте заявку клиента и подготовьте предложение по своему каталогу."/>
  </MarketingPage>;
}
