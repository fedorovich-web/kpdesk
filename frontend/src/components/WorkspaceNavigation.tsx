import { useEffect, useState } from 'react';
import { ClipboardList, FileText, ListChecks, Package, SlidersHorizontal, Users } from 'lucide-react';

const primary = [
  { id: 'workspace-catalog', label: 'Прайс', icon: Package },
  { id: 'workspace-request', label: 'Заявка клиента', icon: ClipboardList, write: true },
  { id: 'workspace-quote-editor', label: 'Товары для КП', icon: ListChecks, write: true },
  { id: 'workspace-quotes', label: 'Коммерческие предложения', icon: FileText },
];
const secondary = [
  { id: 'workspace-company', label: 'Компания и оформление', icon: FileText },
  { id: 'workspace-team', label: 'Команда и доступ', icon: Users },
  { id: 'workspace-sources', label: 'Прайсы и проверка данных', icon: SlidersHorizontal, write: true },
];

export function WorkspaceNavigation({ canWrite }: { canWrite: boolean }) {
  const [active, setActive] = useState(() => window.location.hash || '#workspace-catalog');
  useEffect(() => {
    const update = () => setActive(window.location.hash || '#workspace-catalog');
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  const link = ({ id, label, icon: Icon, write }: (typeof primary)[number]) =>
    !write || canWrite ? <a key={id} href={'#' + id}
      aria-current={active === '#' + id ? 'location' : undefined}
      onClick={() => {
        let target: Element | null = document.getElementById(id);
        while (target) {
          if (target instanceof HTMLDetailsElement) target.open = true;
          target = target.parentElement;
        }
        setActive('#' + id);
      }}><Icon size={18} aria-hidden="true"/><span>{label}</span></a> : null;
  return <nav className="workspace-sidebar" aria-label="Разделы личного кабинета">
    <div className="workspace-nav-group"><span className="workspace-nav-label">Подготовка КП</span>
      {primary.map(link)}</div>
    <div className="workspace-nav-group"><span className="workspace-nav-label">Управление</span>
      {secondary.map(link)}</div>
  </nav>;
}
