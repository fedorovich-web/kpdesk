import { hasActivePublicSession } from './public-session';

/**
 * Minimal progressive enhancement for the prerendered homepage.
 * The SEO copy, links and FAQ answers are already complete HTML. Only the
 * mobile drawer and authenticated header links need a small amount of client JS.
 * All private, demo and search-enabled FAQ routes retain React hydration.
 */
export function enhancePublicHome(doc: Document = document): void {
  const trigger = doc.querySelector<HTMLButtonElement>('.public-header .mobile-menu-toggle');
  const logo = doc.querySelector<HTMLImageElement>('.public-header .public-logo img');
  const nav = doc.querySelector<HTMLElement>('.public-header .public-nav');
  if (!trigger || !logo || !nav) return;

  const create = <K extends keyof HTMLElementTagNameMap>(
    tag: K, className?: string,
  ): HTMLElementTagNameMap[K] => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    return element;
  };
  const icon = (d: string, size: number): SVGSVGElement => {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = doc.createElementNS(ns, 'svg');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const path = doc.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.append(path);
    return svg;
  };
  const layer = create('div', 'mobile-drawer-layer');
  layer.setAttribute('aria-hidden', 'true');
  layer.inert = true;

  const overlay = create('button', 'mobile-drawer-overlay');
  overlay.type = 'button';
  overlay.tabIndex = -1;
  overlay.setAttribute('aria-label', 'Закрыть меню вне панели');

  const drawer = create('aside', 'mobile-drawer');
  drawer.id = trigger.getAttribute('aria-controls') || 'kpdesk-mobile-drawer';
  trigger.setAttribute('aria-controls', drawer.id);
  drawer.setAttribute('role', 'dialog');
  drawer.setAttribute('aria-modal', 'true');
  drawer.setAttribute('aria-label', 'Меню навигации');

  const head = create('div', 'mobile-drawer-head');
  head.append(logo.cloneNode(true));
  const closeButton = create('button', 'mobile-drawer-close');
  closeButton.type = 'button';
  closeButton.setAttribute('aria-label', 'Закрыть меню');
  closeButton.append(icon('M18 6 6 18 M6 6 18 18', 23));
  head.append(closeButton);
  drawer.append(head);

  const links = create('nav', 'mobile-drawer-links');
  links.setAttribute('aria-label', 'Мобильное меню');
  const addLink = (container: HTMLElement, href: string, label: string, current = false) => {
    const anchor = create('a', 'mobile-drawer-link' + (current ? ' current' : ''));
    anchor.href = href;
    if (current) anchor.setAttribute('aria-current', 'page');
    const text = create('span');
    text.textContent = label;
    anchor.append(text, icon('m9 18 6-6-6-6', 19));
    container.append(anchor);
  };
  for (const anchor of nav.querySelectorAll<HTMLAnchorElement>('a[href]')) {
    const href = anchor.getAttribute('href');
    if (href) addLink(links, href, anchor.textContent?.trim() ?? '',
      anchor.getAttribute('aria-current') === 'page');
  }
  addLink(links, '/demo', 'Посмотреть демо');
  addLink(links, '/auth/login', 'Войти');
  drawer.append(links);

  const action = create('div', 'mobile-drawer-action');
  addLink(action, '/app', 'Создать КП');
  drawer.append(action);

  const footer = create('div', 'mobile-drawer-footer');
  const mail = create('a');
  mail.href = 'mailto:support@kpdesk.ru';
  mail.textContent = 'support@kpdesk.ru';
  const support = create('span');
  support.textContent = 'Служба поддержки KPDesk';
  footer.append(mail, support);
  drawer.append(footer);
  layer.append(overlay, drawer);
  doc.body.append(layer);

  // Keep the pre-rendered public HTML static; update only auth links after paint.
  void hasActivePublicSession().then(authenticated => {
    if (!authenticated) return;
    for (const link of doc.querySelectorAll<HTMLAnchorElement>(
      '.public-header-actions a[href="/auth/login"], .mobile-drawer-links a[href="/auth/login"]',
    )) {
      link.setAttribute('href', '/app');
      const label = link.querySelector('span');
      if (label) label.textContent = 'Личный кабинет';
      else link.textContent = 'Личный кабинет';
    }
  });

  let open = false;
  let previousOverflow = '';
  const close = () => {
    if (!open) return;
    open = false;
    layer.classList.remove('is-open');
    layer.inert = true;
    layer.setAttribute('aria-hidden', 'true');
    trigger.setAttribute('aria-expanded', 'false');
    doc.body.style.overflow = previousOverflow;
    trigger.focus();
  };
  trigger.addEventListener('click', () => {
    if (open) return;
    open = true;
    previousOverflow = doc.body.style.overflow;
    doc.body.style.overflow = 'hidden';
    layer.inert = false;
    layer.setAttribute('aria-hidden', 'false');
    layer.classList.add('is-open');
    trigger.setAttribute('aria-expanded', 'true');
    closeButton.focus();
  });
  overlay.addEventListener('click', close);
  closeButton.addEventListener('click', close);
  doc.addEventListener('keydown', event => {
    if (!open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(drawer.querySelectorAll<HTMLElement>('a[href], button:not([disabled])'));
    if (!focusable.length) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && doc.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && doc.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
}

if (typeof document !== 'undefined' && document.querySelector('#root[data-ssr-route="/"]')) {
  enhancePublicHome();
}
