import { Check } from 'lucide-react';
import { launchPlans, count, rubles } from '../content/pricing';
import { designAssets, publicIconSrcSet } from '../design-assets';

/** Reference #05 composition, with explicit planned launch capacities. */
export function PublicPricingCards() {
  const icons = { document: designAssets.icon.createQuote, database: designAssets.icon.database,
    analytics: designAssets.icon.analytics };
  return <div className="public-plan-grid" role="group" aria-label="Планируемые тарифы KPDesk">
    {launchPlans.map((option, index) => <article className={'public-plan-card' + (index === 1 ? ' public-plan-featured' : '')} key={option.id}>
      {index === 1 && <span className="public-plan-badge">Для совместной работы</span>}
      <div className="public-plan-heading"><img src={icons[option.icon]} srcSet={publicIconSrcSet(icons[option.icon])} sizes="48px" alt="" width={48} height={48} loading="lazy"/>
        <div><h3>{option.name}</h3><p>{option.description}</p></div></div>
      <div className="public-plan-cost"><strong className="public-plan-price">{rubles(option.priceRub)}</strong>
        <span> / месяц</span><small>за организацию · планируемая цена</small></div>
      <ul>{[option.people, `${count(option.newQuotes)} новых КП в месяц`, `${count(option.catalogItems)} товаров в каталоге`,
        'Подбор, расчёт и текст КП', 'Реквизиты, логотип и PDF', 'История предложений'].map(feature =>
        <li key={feature}><Check size={18} aria-hidden="true"/>{feature}</li>)}</ul>
      <a className={'button ' + (index === 1 ? 'button-primary' : 'button-white')}
        href="/app">Начать работу</a>
    </article>)}
  </div>;
}
