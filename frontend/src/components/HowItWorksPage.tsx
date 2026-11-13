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
      title="Как создать КП из заявки и прайса"
      description="Загрузите товары и цены, вставьте заявку клиента. ИИ разберёт требования, KPDesk предложит товары и рассчитает стоимость. Затем сгенерируйте текст, проверьте предложение и скачайте PDF после утверждения."
      image={designAssets.processHero}
      imageAlt="Иллюстрация пяти этапов подготовки коммерческого предложения"
      anchor="#process" secondaryLabel="Посмотреть этапы"
      highlights={['Разбор заявки с ИИ', 'Подбор из вашего прайса', 'Текст и расчёт в PDF']}
    />
    <section className="marketing-step-ribbon" aria-label="Пять шагов">
      {processSteps.map(({ title }, index) => <div className="marketing-ribbon-step" key={title}>
        <span className="marketing-step-number">{index + 1}</span>
        <h2>{title}</h2>
        <p>{[
          'Сохраните товары и цены для подбора.',
          'Проверьте требования, которые выделил ИИ.',
          'Выберите из предложенных позиций и проверьте сумму.',
          'Получите текст по товарам и своим условиям.',
          'Проверьте документ и утвердите его для клиента.',
        ][index]}</p>
      </div>)}
    </section>

    <MarketingSection id="process" heading="Пять шагов от прайса до утверждённого КП">
      <ol className="marketing-step-details">{processSteps.map(({ title, text }, index) =>
        <li key={title} className="marketing-step-detail">
          <div className="marketing-step-title"><span className="marketing-step-number">{index + 1}</span><h3>{title}</h3></div>
          <p>{text}</p>
          <div className="marketing-step-art"><img
            src={'/assets/reference/' + ['process-request', 'process-template', 'process-products', 'process-review', 'process-pdf'][index] + '.webp'}
            width={182} height={186} alt="Иллюстрация этапа из утверждённого макета" loading="lazy"/></div>
        </li>)}</ol>
    </MarketingSection>

    <MarketingSection heading="Меньше переносов между прайсом и документом" modifier="marketing-benefits-section">
      <div className="marketing-benefits">{processBenefits.map(({ title, text, icon }) =>
        <article key={title}>
          <img src={icon} alt="" width={50} height={50} loading="lazy"/>
          <div><h3>{title}</h3><p>{text}</p></div>
        </article>)}</div>
    </MarketingSection>
    <section className="marketing-question-row">
      <div><h2>Готовите первое предложение?</h2><p>В справке разобраны формат прайса, работа с заявкой, генерация текста и утверждение PDF.</p></div>
      <a href="/faq" className="button button-white">Открыть справку <ArrowRight size={17} aria-hidden="true"/></a>
    </section>
    <MarketingCta title="Создайте своё первое КП"
      description="Начните с прайса и заявки клиента. KPDesk поможет подобрать товары, рассчитать стоимость и подготовить PDF." />
  </MarketingPage>;
}
