import { useEffect } from 'react';
import { ArrowRight, CheckCircle2, X } from 'lucide-react';
import { designAssets } from '../design-assets';
import { featureDetails, featureTiles } from '../content/marketing';
import { applyDocumentSeo } from '../seo';
import { MarketingCta, MarketingHero, MarketingPage, MarketingSection } from './MarketingShared';

const featureIllustrations = [
  '/assets/supporting/kpdesk_offer_features.png',
  '/assets/supporting/kpdesk_offer_stack.png',
  designAssets.benefitsHero,
] as const;
const featureActions = [
  { href: '/app#workspace-request', label: 'Добавить заявку' },
  { href: '/app#workspace-catalog', label: 'Загрузить прайс' },
  { href: '/app#workspace-quote-editor', label: 'Подобрать товары' },
  { href: '/app#workspace-quotes', label: 'Подготовить текст КП' },
  { href: '/app#workspace-quotes', label: 'Добавить варианты КП' },
  { href: '/app#workspace-quotes', label: 'Открыть свои КП' },
] as const;

export function FeaturesPage() {
  useEffect(() => applyDocumentSeo('features'), []);
  return <MarketingPage>
    <MarketingHero
      title="ИИ-генерация текста КП и подбор товаров"
      description="Соберите предложение из письма клиента и своего прайса. ИИ разберёт заявку и подготовит текст, KPDesk предложит товары и рассчитает сумму. Проверьте результат и скачайте КП с реквизитами и логотипом компании."
      image={designAssets.featuresHero}
      imageAlt="Иллюстрация подготовки коммерческого предложения в KPDesk"
      anchor="#capabilities" secondaryLabel="Посмотреть возможности"
      highlights={['Автоматический подбор', 'Генерация текста КП', 'PDF после утверждения']}
    />
    <MarketingSection id="capabilities" heading="Что KPDesk берёт на себя при подготовке КП">
      <div className="marketing-capabilities">
        {featureTiles.map((tile) => <article className="marketing-capability" key={tile.title}>
          <img src={tile.icon} alt="" loading="lazy" width={48} height={48}/>
          <h3>{tile.title}</h3><p>{tile.description}</p>

        </article>)}
      </div>
    </MarketingSection>

    <MarketingSection heading="Как ваши данные становятся предложением для клиента" modifier="marketing-details-section">
      <div className="marketing-detail-grid">
        {featureDetails.map(({ title, description, icon }, index) => <article className="marketing-detail" key={title}>
          <div className="marketing-detail-visual">
            {index < featureIllustrations.length
              ? <img src={featureIllustrations[index]} alt="" loading="lazy"/>
              : <img src={icon} alt="" loading="lazy" width={96} height={96}/>}
          </div>
          <div><h3>{title}</h3><p>{description}</p>
            <a href={featureActions[index]!.href}>
              {featureActions[index]!.label} <ArrowRight size={16} aria-hidden="true"/>
            </a>
          </div>
        </article>)}
      </div>
    </MarketingSection>

    <MarketingSection heading="Меньше ручной работы с каждым КП" modifier="marketing-comparison">
      <div className="marketing-comparison-grid">
        <div><h3>Когда КП собирают вручную</h3>
          {['Ищут каждый товар в таблицах', 'Копируют цены в документ', 'Пишут текст с чистого листа', 'Хранят версии в отдельных файлах'].map(text =>
            <p key={text}><X size={18} aria-hidden="true"/>{text}</p>)}</div>
        <div><h3>В KPDesk</h3>
          {['Товары из каталога по требованиям заявки', 'Автоматический расчёт по ценам из прайса', 'Генерация текста по товарам и условиям', 'Сохранённые КП и история изменений'].map(text =>
            <p key={text}><CheckCircle2 size={18} aria-hidden="true"/>{text}</p>)}</div>
      </div>
    </MarketingSection>
    <MarketingCta title="Подготовьте предложение для клиента"
      description="Добавьте прайс и заявку. Подберите товары, проверьте текст и суммы — скачайте КП с логотипом компании." />
  </MarketingPage>;
}
