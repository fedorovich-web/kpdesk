import { useEffect } from 'react';
import { ArrowRight, Check, MoreHorizontal } from 'lucide-react';
import { applyDocumentSeo, publicSections, publicSeo } from '../seo';
import { homeFaqs } from '../content/faq';
import { PublicHeader, PublicFooter } from './PublicChrome';
import { designAssets } from '../design-assets';
import { PublicPricingCards } from './PublicPricingCards';
import { launchPlans, rubles } from '../content/pricing';
import { FaqAccordion } from './FaqAccordion';

const advantages = [
  { icon: designAssets.icon.speed, title: 'Товары и цены без копирования', text: 'Загрузите прайс один раз и используйте каталог в следующих КП. Обновляйте его, когда меняются цены.' },
  { icon: designAssets.icon.success, title: 'Из письма — в список товаров', text: 'ИИ выделит позиции и требования из заявки. KPDesk предложит товары из вашего прайса для проверки и выбора.' },
  { icon: designAssets.icon.analytics, title: 'Расчёт без ручных формул', text: 'Выберите товары и количество. Стоимость каждой позиции и общая сумма рассчитаются автоматически.' },
  { icon: designAssets.icon.team, title: 'Предложение от вашей компании', text: 'Получите текст с помощью ИИ, добавьте реквизиты и логотип. Проверьте документ и скачайте готовое КП в PDF.' },
] as const;

const steps = [
  { title: 'Загрузите товары и цены', body: 'Добавьте прайс в CSV или XLSX и проверьте данные каталога.' },
  { title: 'Получите подбор по заявке', body: 'Вставьте текст клиента. Проверьте требования, которые выделил ИИ, и подтвердите предложенные товары.' },
  { title: 'Сгенерируйте текст и скачайте КП', body: 'Добавьте условия для ИИ. Проверьте текст, товары и рассчитанные суммы. Утвердите КП и скачайте PDF.' },
] as const;

export function PublicHome() {
  useEffect(() => applyDocumentSeo('public'), []);
  return <div className="public-page">
    <PublicHeader />
    <main>
      <section className="public-hero" aria-labelledby="public-h1">
        <div className="public-hero-copy">
          <h1 id="public-h1">{publicSeo.h1}</h1>
          <p>{publicSeo.lead}</p>
          <div className="public-hero-cta">
            <a className="button button-primary" href="/demo">Посмотреть демо <ArrowRight size={18} aria-hidden="true"/></a>
            <a className="button button-white" href="#how">Как это работает</a>
          </div>
          <ul className="public-proof">
            <li><Check size={18} aria-hidden="true"/> Цены из вашего прайса</li>
            <li><Check size={18} aria-hidden="true"/> Автоматический расчёт</li>
            <li><Check size={18} aria-hidden="true"/> PDF с логотипом компании</li>
          </ul>
        </div>
        <img className="public-hero-art" src={designAssets.publicHero}
          alt="Иллюстрация редактора заявки и коммерческого предложения KPDesk"
          width={1448} height={1086} decoding="async"/>
      </section>

      <section className="public-advantages" aria-label="Подготовка коммерческого предложения">
        {advantages.map(({ icon, title, text }) =>
          <article className="public-advantage" key={title}>
            <span className="public-icon"><img src={icon} alt="" width={48} height={48} loading="lazy"/></span>
            <div><h3>{title}</h3><p>{text}</p></div>
          </article>)}
      </section>

      <section className="public-section public-feature-section" id="features">
        <h2>{publicSections[0].heading}</h2>
        <p className="public-section-lead">Работайте с предложенными товарами и готовым текстом: KPDesk подбирает позиции и считает стоимость, ИИ разбирает заявку и генерирует содержание КП. Вы проверяете результат и утверждаете документ для клиента.</p>
        <div className="public-feature-grid">
          {publicSections[0].items.map(({ heading, body }, index) => {
            const icon = [
              designAssets.icon.createQuote, designAssets.icon.database,
              designAssets.icon.settings, designAssets.icon.team,
            ][index];
            return <article className="public-feature-card" key={heading}>
              {icon && <span className="public-icon"><img src={icon} alt="" width={48} height={48} loading="lazy"/></span>}
              <h3>{heading}</h3><p>{body}</p>
            </article>;
          })}
        </div>
      </section>

      <section className="public-section public-how-section" id="how">
        <h2>Как создать первое КП</h2>
        <ol className="public-step-grid">
          {steps.map(({title,body}, index) =>
            <li className="public-step" key={title}>
              <span className="public-step-index">{index+1}</span>
              <div><h3>{title}</h3><p>{body}</p></div>
            </li>)}
        </ol>
        <img className="public-how-illustration" src={designAssets.offerStack}
          alt="Иллюстрация документов коммерческого предложения"
          width={1672} height={941} loading="lazy"/>
      </section>

      <section className="public-section" id="integrations">
        <h2>Загрузите прайс для подбора и расчёта КП</h2>
        <p className="public-section-lead">Названия, характеристики и цены берутся из вашего каталога. Загрузите CSV или Excel (.xlsx) в формате KPDesk; для данных из 1С или CRM подготовьте совместимую выгрузку. Используйте каталог в следующих предложениях и обновляйте его при смене цен.</p>
        <div className="public-integration-grid">
          {[
            ['1С', '1c', 'Выгрузка в CSV/XLSX'],
            ['Битрикс24', 'bitrix24', 'Через выгрузку товаров'],
            ['amoCRM', 'amocrm-display', 'Таблица товаров'],
            ['Excel', 'microsoft-excel-display', 'Импорт прайса'],
            ['Google Диск', 'google-drive', 'Через скачанный файл'],
          ].map(([name, asset, description]) => <div key={name}>
            <span className="brand-logo-frame"><img src={'/assets/brands/' + asset + '.svg'} alt={'Логотип ' + name} width={128} height={48} loading="lazy"/></span>
            <span><strong>{name}</strong><small>{description}</small></span>
          </div>)}
          <div><span className="brand-logo-frame brand-logo-frame-other"><MoreHorizontal size={28} aria-hidden="true"/></span><span><strong>Другие системы</strong><small>Совместимый CSV/XLSX</small></span></div>
        </div>
      </section>

      <section className="public-section public-pricing-section" id="pricing" aria-labelledby="home-pricing-title">
        <h2 id="home-pricing-title">Тарифы для вашей команды</h2>
        <p>К запуску подписок — от {rubles(launchPlans[0].priceRub)} в месяц за организацию. <a href="/pricing">Сравнить тарифы</a>.</p>
        <PublicPricingCards />
      </section>

      <section className="public-section public-faq-section" id="faq" aria-labelledby="home-faq-title">
        <h2 id="home-faq-title">Вопросы о работе с KPDesk</h2>
        <p className="public-section-lead">Как подготовить прайс, проверить подбор и текст ИИ, утвердить предложение и скачать PDF.</p>
        <FaqAccordion items={homeFaqs} className="public-faq-list" itemClassName="public-faq-item" />
        <p><a href="/faq">Все вопросы и ответы</a></p>
      </section>

      <section className="public-end-cta">
        <div><h2>Подготовьте КП для своего клиента</h2>
          <p>Загрузите прайс и заявку, проверьте товары и суммы — сохраните предложение в PDF.</p></div>
        <div className="marketing-cta-next"><div className="marketing-cta-actions">
          <a href="/app" className="button button-white">Создать КП <ArrowRight size={18} aria-hidden="true"/></a>
          <a href="/demo" className="button button-outline-light">Посмотреть демо</a>
        </div><ul className="public-cta-proof">
          <li><Check size={15} aria-hidden="true"/>Цены из вашего прайса</li>
          <li><Check size={15} aria-hidden="true"/>Расчёт и PDF</li>
        </ul></div>
      </section>
    </main>
    <PublicFooter />
  </div>;
}
