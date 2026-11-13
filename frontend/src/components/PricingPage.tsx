import { useEffect } from 'react';
import { designAssets } from '../design-assets';
import { Check } from 'lucide-react';
import { launchPlans, count, pricingFaqs, rubles } from '../content/pricing';
import { applyDocumentSeo } from '../seo';
import { MarketingCta, MarketingHero, MarketingPage, MarketingSection } from './MarketingShared';
import { PublicPricingCards } from './PublicPricingCards';

const included = [
  { title: 'Каталог из вашего прайса', text: 'Загрузите CSV/XLSX, чтобы подбирать товары и считать стоимость по своим данным.', icon: designAssets.icon.upload },
  { title: 'Подбор товаров и расчёт', text: 'Получите позиции по заявке, подтвердите выбор и проверьте рассчитанные суммы.', icon: designAssets.icon.database },
  { title: 'Разбор заявки и текст с ИИ', text: 'Выделите требования из письма и сгенерируйте текст по товарам и условиям. Обращения к ИИ ограничены отдельно.', icon: designAssets.icon.documentInfo },
  { title: 'Оформление и PDF', text: 'Добавьте реквизиты, логотип и цвет компании. Скачайте PDF после проверки и утверждения.', icon: designAssets.icon.quotes },
  { title: 'История КП и версий', text: 'Возвращайтесь к предложениям с сохранёнными товарами, текстом и суммами.', icon: designAssets.icon.checkQuote },
  { title: 'Права сотрудников', text: 'Работайте с данными компании в общем кабинете и определяйте доступ каждого сотрудника.', icon: designAssets.icon.team },
] as const;
const comparison = [
  { label: 'Сотрудники в организации', values: launchPlans.map(plan => String(plan.peopleLimit)) },
  { label: 'Новые КП за месяц', values: launchPlans.map(plan => count(plan.newQuotes)) },
  { label: 'Товары в каталоге', values: launchPlans.map(plan => count(plan.catalogItems)) },
  ...['Прайс CSV/XLSX', 'Подбор товаров и расчёт', 'Реквизиты и логотип',
    'Варианты предложения и PDF', 'История и утверждение КП'].map(label => ({ label, values: null })),
];

export function PricingPage() {
  useEffect(() => applyDocumentSeo('pricing'), []);
  return <MarketingPage modifier="pricing-page">
    <MarketingHero
      title="Тарифы KPDesk"
      description={`К запуску подписок — от ${rubles(launchPlans[0].priceRub)} в месяц за организацию. Подбор товаров, расчёт, генерация текста и PDF предусмотрены в каждом тарифе. Сравните число сотрудников, товаров и новых КП. Оплата пока не подключена.`}
      image={designAssets.pricingHero}
      imageAlt="Иллюстрация работы с коммерческими предложениями"
      anchor="#pricing-plans" secondaryLabel="Сравнить тарифы"
      highlights={['Цена за организацию', 'Основные функции в каждом тарифе', 'Демо без карты']}
    />
    <MarketingSection id="pricing-plans" heading="Выберите тариф для своей команды" modifier="pricing-plans-section">
      <p className="pricing-lead">Оцените размер каталога, число сотрудников и сколько новых предложений команда готовит за месяц. Обращения к ИИ ограничены отдельно от числа новых КП.</p>
      <PublicPricingCards/>
    </MarketingSection>
    <MarketingSection heading="Сравнение тарифов" modifier="pricing-comparison-section">
      <p className="pricing-table-hint" id="pricing-table-help">Лимиты ниже начнут применяться после запуска подписок. На узком экране таблицу можно прокрутить вправо.</p>
      <section className="pricing-table-scroll" aria-label="Таблица условий тарифов" aria-describedby="pricing-table-help" tabIndex={0}>
        <table className="pricing-table" aria-label="Сравнение планируемых тарифов KPDesk">
          <thead><tr><th scope="col">Возможности</th>{launchPlans.map(plan => <th scope="col" key={plan.id}>{plan.name}</th>)}</tr></thead>
          <tbody>{comparison.map(row => <tr key={row.label}><th scope="row">{row.label}</th>
            {launchPlans.map((plan,index) => <td key={plan.id}>{row.values ? row.values[index]
              : <><Check size={19} aria-hidden="true"/><span className="sr-only">Включено</span></>}</td>)}</tr>)}</tbody>
        </table>
      </section>
      <p className="pricing-table-footnote">Лимит относится к новым документам. Редакции и повторное скачивание PDF не расходуют лимит новых КП.</p>
    </MarketingSection>
    <MarketingSection heading="Что входит в каждый тариф" modifier="pricing-included-section">
      <div className="pricing-included-grid">
        {included.map(({title, text, icon}) => <article key={title}>
          <img src={icon} alt="" width={48} height={48} loading="lazy"/>
          <div><h3>{title}</h3><p>{text}</p></div>
        </article>)}
      </div>
    </MarketingSection>
    <MarketingSection heading="Вопросы о стоимости" modifier="marketing-price-faq">
      <div className="faq-questions">{pricingFaqs.map(({q,a}) =>
        <details key={q}><summary>{q}</summary><p>{a}</p></details>)}</div>
    </MarketingSection>
    <MarketingCta title="Попробуйте демо перед выбором тарифа"
      description="Посмотрите подбор и расчёт на учебных товарах. Регистрация и банковская карта не нужны."/>
  </MarketingPage>;
}
