import { ArrowRight } from 'lucide-react';
import { designAssets } from '../design-assets';
import { MobileNavigation } from './MobileNavigation';

export function PublicHeader() {
  return <header className="public-header">
    <a className="public-logo" href="/" aria-label="KPDesk — главная">
      <img src={designAssets.logo} alt="KPDesk" width={203} height={52}/>
    </a>
    <nav className="public-nav" aria-label="Основная навигация">
      <a href="/features" aria-current={window.location.pathname === '/features' ? 'page' : undefined}>Возможности</a>
      <a href="/how-it-works" aria-current={window.location.pathname === '/how-it-works' ? 'page' : undefined}>Как это работает</a>
      <a href="/integrations" aria-current={window.location.pathname === '/integrations' ? 'page' : undefined}>Прайсы и данные</a>
      <a href="/pricing" aria-current={window.location.pathname === '/pricing' ? 'page' : undefined}>Тарифы</a>
      <a href="/faq" aria-current={window.location.pathname === '/faq' ? 'page' : undefined}>Вопросы и ответы</a>
    </nav>
    <div className="public-header-actions">
      <a className="button button-white" href="/auth/login">Войти</a>
      <a className="button button-primary public-header-cta" href="/demo">
        Открыть демо <ArrowRight size={18} aria-hidden="true"/>
      </a>
    </div>
    <MobileNavigation links={[
      { href: '/features', label: 'Возможности', current: window.location.pathname === '/features' },
      { href: '/how-it-works', label: 'Как это работает', current: window.location.pathname === '/how-it-works' },
      { href: '/integrations', label: 'Прайсы и данные', current: window.location.pathname === '/integrations' },
      { href: '/pricing', label: 'Тарифы', current: window.location.pathname === '/pricing' },
      { href: '/faq', label: 'Вопросы и ответы', current: window.location.pathname === '/faq' },
      { href: '/auth/login', label: 'Войти', current: window.location.pathname === '/auth/login' },
    ]} action={{ href: '/demo', label: 'Открыть демо' }}/>
  </header>;
}

export function PublicFooter() {
  return <footer className="public-footer">
    <div className="public-footer-brand">
      <a href="/" aria-label="На главную"><img src={designAssets.logo} alt="KPDesk" width={203} height={52}/></a>
      <p>ИИ разбирает заявку и пишет текст КП. KPDesk подбирает товары из вашего прайса, считает стоимость и собирает PDF для проверки и утверждения.</p>
      <small className="public-copyright copyright">© 2026 KPDesk</small>
    </div>
    <nav aria-label="Разделы продукта" className="public-footer-column">
      <h2>Продукт</h2>
      <a href="/features">Возможности</a>
      <a href="/how-it-works">Как это работает</a>
      <a href="/integrations">Прайсы и данные</a>
      <a href="/pricing">Тарифы</a>
    </nav>
    <nav aria-label="Помощь и ресурсы" className="public-footer-column">
      <h2>Помощь</h2>
      <a href="/faq">Вопросы и ответы</a>
      <a href="mailto:support@kpdesk.ru">support@kpdesk.ru</a>
      <a href="/demo">Демонстрация</a>
    </nav>
    <div className="public-footer-column">
      <h2>Документы</h2>
      <a href="/privacy">Политика конфиденциальности</a>
      <p>Онлайн-сервис для подготовки коммерческих предложений.</p>
    </div>
    <span className="public-footer-language" lang="ru">Русский</span>
  </footer>;
}
