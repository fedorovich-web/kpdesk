import { useEffect } from 'react';
import { ArrowRight } from 'lucide-react';
import { designAssets } from '../design-assets';
import { processBenefits, processSteps } from '../content/marketing';
import { applyDocumentSeo } from '../seo';
import { MarketingCta, MarketingHero, MarketingPage, MarketingSection } from './MarketingShared';

export function HowItWorksPage() {
  useEffect(() => applyDocumentSeo('how'), []);
  return <MarketingPage>
    <MarketingHero
      title="Как собрать КП из заявки и прайса"
      description="Для первого КП подготовьте прайс в CSV или XLSX, текст заявки и реквизиты компании. Ниже пять шагов от загрузки данных до проверки предложения и скачивания PDF."
      image={designAssets.processHero}
      imageAlt="Иллюстрация пяти этапов подготовки коммерческого предложения"
      anchor="#process" secondaryLabel="Посмотреть этапы"
      highlights={['Прайс CSV или XLSX', 'Проверка требований и товаров', 'Предпросмотр перед скачиванием']}
    />
    <section className="marketing-step-ribbon" aria-label="Пять шагов">
      {processSteps.map(({ title }, index) => <div className="marketing-ribbon-step" key={title}>
        <span className="marketing-step-number">{index + 1}</span>
        <strong>{title}</strong>
        <p>{[
          'Сохраните товары и цены для подбора.',
          'Проверьте требования, которые выделил ИИ.',
          'Выберите из предложенных позиций и проверьте сумму.',
          'Получите текст по товарам и своим условиям.',
          'Проверьте документ и утвердите его для клиента.',
        ][index]}</p>
      </div>)}
    </section>

    <MarketingSection id="process" heading="Пять шагов от загрузки прайса до готового PDF">
      <ol className="marketing-step-details">{processSteps.map(({ title, text, icon, preview }, index) =>
        <li key={title} className="marketing-step-detail">
          <div className="marketing-step-title"><span className="marketing-step-number">{index + 1}</span><h3>{title}</h3></div>
          <p>{text}</p>
          <div className="marketing-step-art" aria-hidden="true">
            <div className="marketing-step-preview-title">
              <img src={icon} alt="" width={24} height={24} loading="lazy"/>
              <strong>{preview.title}</strong>
            </div>
            <dl>{preview.rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
          </div>
        </li>)}</ol>
    </MarketingSection>

    <MarketingSection heading="Что проверить и сохранить перед отправкой КП" modifier="marketing-benefits-section">
      <div className="marketing-benefits">{processBenefits.map(({ title, text, icon }) =>
        <article key={title}>
          <img src={icon} alt="" width={50} height={50} loading="lazy"/>
          <div><h3>{title}</h3><p>{text}</p></div>
        </article>)}</div>
    </MarketingSection>
    <section className="marketing-question-row">
      <div><h2>Справка по прайсу, ИИ и PDF</h2><p>В справке разобраны формат прайса, работа с заявкой, генерация текста и утверждение PDF.</p></div>
      <a href="/faq" className="button button-white">Открыть справку <ArrowRight size={17} aria-hidden="true"/></a>
    </section>
    <MarketingCta title="Создайте своё первое КП"
      description="Начните с прайса и заявки клиента. KPDesk поможет подобрать товары, рассчитать стоимость и подготовить PDF." />
  </MarketingPage>;
}
