import { useEffect } from 'react';
import { ArrowRight, CircleHelp, ChartColumn, House, LayoutGrid } from 'lucide-react';
import { designAssets } from '../design-assets';
import { applyDocumentSeo } from '../seo';
import { MarketingPage } from './MarketingShared';

const suggestions = [
  { path: '/', label: 'Главная', description: 'ИИ-генератор КП с подбором товаров', icon: House },
  { path: '/features', label: 'Возможности', description: 'Разбор заявки, подбор и текст КП', icon: LayoutGrid },
  { path: '/pricing', label: 'Тарифы', description: 'Цены, сотрудники, товары и новые КП', icon: ChartColumn },
  { path: '/faq', label: 'Вопросы и ответы', description: 'Прайсы, ИИ, утверждение и PDF', icon: CircleHelp },
] as const;

export function NotFoundPage() {
  useEffect(() => applyDocumentSeo('notFound', 'Страница не найдена'), []);
  return <MarketingPage>
    <section className="not-found-hero">
      <div>
        <h1><span className="not-found-code">404</span><span>Страница не найдена</span></h1>
        <p>Возможно, адрес изменился или ссылка устарела. Перейдите на главную страницу либо откройте нужный раздел.</p>
        <div className="public-hero-cta">
          <a className="button button-primary" href="/">На главную <ArrowRight size={18} aria-hidden="true"/></a>
          <a className="button button-white" href="/faq">Вопросы и ответы</a>
        </div>
      </div>
      <img src={designAssets.notFoundHero} alt="Иллюстрация страницы 404 с разорванной ссылкой"
        width={1448} height={1086} decoding="async"/>
    </section>
    <section className="marketing-section not-found-suggestions">
      <h2>Перейдите к нужному разделу</h2>
      <p>Узнайте, как подготовить КП, сравните тарифы или найдите ответ в справке.</p>
      <div className="not-found-links">{suggestions.map(({ path, label, description, icon: Icon }) =>
        <a href={path} key={path}>
          <span className="not-found-link-icon"><Icon size={27} aria-hidden="true"/></span>
          <span><strong>{label}</strong><small>{description}</small></span>
          <ArrowRight size={18} aria-hidden="true"/>
        </a>)}</div>
    </section>
  </MarketingPage>;
}
