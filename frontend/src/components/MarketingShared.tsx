import type { ReactNode } from 'react';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { PublicFooter, PublicHeader } from './PublicChrome';

type HeroProps = {
  title: string;
  description: string;
  image: string;
  imageAlt: string;
  anchor: string;
  secondaryLabel: string;
  highlights: readonly string[];
};
export function MarketingHero({
  title, description, image, imageAlt, anchor, secondaryLabel, highlights,
}: HeroProps) {
  return <section className="marketing-hero">
    <div className="marketing-hero-text">
      <h1>{title}</h1>
      <p>{description}</p>
      <div className="public-hero-cta">
        <a href="/demo" className="button button-primary">Попробовать демо <ArrowRight size={18} aria-hidden="true"/></a>
        <a href={anchor} className="button button-white">{secondaryLabel}</a>
      </div>
      <ul className="public-proof">{highlights.map((item) => <li key={item}>
        <CheckCircle2 size={18} aria-hidden="true"/>{item}</li>)}</ul>
    </div>
    <img src={image} alt={imageAlt} width={1448} height={1086} loading="eager" decoding="async" fetchPriority="high" />
  </section>;
}

export function MarketingCta({ title, description }: { title: string; description: string }) {
  return <section className="marketing-cta" aria-label="Работа с KPDesk">
    <div><h2>{title}</h2><p>{description}</p></div>
    <div className="marketing-cta-next"><div className="marketing-cta-actions">
      <a className="button button-white" href="/app">Открыть кабинет <ArrowRight size={17} aria-hidden="true"/></a>
      <a className="button button-outline-light" href="/demo">Попробовать демо</a>
    </div><ul className="public-cta-proof">
      <li><CheckCircle2 size={15} aria-hidden="true"/>Ваш прайс</li>
      <li><CheckCircle2 size={15} aria-hidden="true"/>Расчёт стоимости</li>
      <li><CheckCircle2 size={15} aria-hidden="true"/>Готовый PDF</li>
    </ul></div>
  </section>;
}

export function MarketingPage({ children, modifier }: { children: ReactNode; modifier?: string }) {
  return <div className={'public-page' + (modifier ? ' ' + modifier : '')}>
    <PublicHeader />
    <main>{children}</main>
    <PublicFooter />
  </div>;
}

export function MarketingSection({ id, heading, children, modifier }: {
  id?: string; heading: string; children: ReactNode; modifier?: string;
}) {
  return <section className={'marketing-section ' + (modifier ?? '')} id={id}>
    <h2>{heading}</h2>
    {children}
  </section>;
}
