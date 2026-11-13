import { useEffect, useId, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { ChevronRight, Menu, X } from 'lucide-react';
import { designAssets } from '../design-assets';

export interface NavigationLink {
  href: string;
  label: string;
  current?: boolean;
}
interface Props {
  links: readonly NavigationLink[];
  label?: string;
  onNavigate?: (href: string) => void;
  action?: NavigationLink;
}

export function MobileNavigation({ links, label = 'Открыть меню', onNavigate, action }: Props) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const dialogId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  useEffect(() => setMounted(true), []);

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
      if (event.key !== 'Tab') return;
      const elements = [...(drawerRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled])',
      ) ?? [])];
      if (!elements.length) return;
      const first = elements[0]!;
      const last = elements[elements.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    const media = window.matchMedia?.('(min-width: 769px)');
    const onDesktop = () => { if (media?.matches) close(); };
    media?.addEventListener('change', onDesktop);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', onKeyDown);
      media?.removeEventListener('change', onDesktop);
    };
  }, [open]);

  const follow = (event: MouseEvent<HTMLAnchorElement>, href: string) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    setOpen(false);
    if (onNavigate) {
      event.preventDefault();
      onNavigate(href);
      triggerRef.current?.focus();
    }
  };
  const renderLink = ({ href, label: text, current }: NavigationLink) =>
    <a key={href} href={href} aria-current={current ? 'page' : undefined}
      className={'mobile-drawer-link' + (current ? ' current' : '')}
      onClick={event => follow(event, href)}>
      <span>{text}</span><ChevronRight size={19} aria-hidden="true"/>
    </a>;

  return <>
    <button ref={triggerRef} type="button" className="mobile-menu-toggle"
      aria-label={label} aria-controls={dialogId} aria-expanded={open}
      onClick={() => setOpen(true)}>
      <Menu size={24} aria-hidden="true"/>
    </button>
    {mounted && createPortal(<div className={'mobile-drawer-layer' + (open ? ' is-open' : '')}
      aria-hidden={!open} inert={!open}>
      <button type="button" tabIndex={open ? 0 : -1} className="mobile-drawer-overlay"
        aria-label="Закрыть меню вне панели" onClick={close}/>
      <aside ref={drawerRef} id={dialogId} className="mobile-drawer"
        role="dialog" aria-modal="true" aria-label="Меню навигации">
        <div className="mobile-drawer-head">
          <img src={designAssets.logo} width={203} height={52} alt="KPDesk"/>
          <button ref={closeRef} type="button" className="mobile-drawer-close"
            aria-label="Закрыть меню" onClick={close}><X size={23} aria-hidden="true"/></button>
        </div>
        <nav aria-label="Мобильное меню" className="mobile-drawer-links">
          {links.map(renderLink)}
        </nav>
        {action && <div className="mobile-drawer-action">{renderLink(action)}</div>}
        <div className="mobile-drawer-footer">
          <a href="mailto:support@kpdesk.ru">support@kpdesk.ru</a>
          <span>Служба поддержки KPDesk</span>
        </div>
      </aside>
    </div>, document.body)}
  </>;
}
