import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PrivacyPage } from './PrivacyPage';

function head() {
  document.head.innerHTML = [
    '<meta name="description">',
    '<meta name="robots">',
    '<meta property="og:title">',
    '<meta property="og:description">',
  ].join('');
}

describe('privacy document for a natural-person operator', () => {
  it('identifies the individual operator accurately and gives an actual contact channel', () => {
    head();
    window.history.replaceState({}, '', '/privacy');
    render(<PrivacyPage/>);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Политика конфиденциальности');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByText('физическое лицо Федорович Р. О.')).toBeInTheDocument();
    expect(screen.getByText(/физическое лицо/i)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'support@kpdesk.ru' })[0])
      .toHaveAttribute('href', 'mailto:support@kpdesk.ru');
    expect(document.title).toMatch(/Политика конфиденциальности/);
  });

  it('states remaining verification gates instead of fabricating operator address or claiming launch', () => {
    head();
    window.history.replaceState({}, '', '/privacy');
    render(<PrivacyPage/>);
    expect(screen.getByText(/Проект документа/i)).toBeInTheDocument();
    expect(screen.queryByText(/почтовый адрес оператора/i)).not.toBeInTheDocument();
    const contacts = screen.getAllByRole('link', { name: 'support@kpdesk.ru' });
    const emails = [...document.querySelectorAll<HTMLAnchorElement>('a[href^="mailto:"]')]
      .map(link => link.href.replace(/^mailto:/, '').toLowerCase());
    expect(new Set(emails)).toEqual(new Set(['support@kpdesk.ru']));
    expect(contacts.length).toBeGreaterThan(0);
    for (const link of contacts) expect(link).toHaveAttribute('href', 'mailto:support@kpdesk.ru');
    expect(screen.queryByText(/no-replay@kpdesk.ru|no-reply@kpdesk.ru/i)).not.toBeInTheDocument();
    expect(screen.getByText(/место размещения баз данных/i)).toBeInTheDocument();
    expect(screen.getByText(/учётные записи, роли и приглашения/i)).toBeInTheDocument();
    expect(screen.queryByText(/Регистрация пользователей.*в разработке/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/трансграничн/i).length).toBeGreaterThan(0);
    expect(document.querySelector('meta[name="robots"]')).toHaveAttribute('content', 'noindex,follow');
    expect(screen.queryByText(/ООО|ОГРН|ИП «Федорович»/i)).not.toBeInTheDocument();
  });

  it('discloses real-catalog AI processing while retaining draft noindex', () => {
    head();
    render(<PrivacyPage/>);
    const disclosure = document.querySelector('[aria-labelledby="privacy-processors"]');
    expect(disclosure).toHaveTextContent('OpenRouter');
    expect(disclosure).toHaveTextContent('Nemotron');
    expect(disclosure).toHaveTextContent(/реальные прайсы/);
    expect(disclosure).toHaveTextContent(/С явным согласием пользователя/);
    expect(disclosure).toHaveTextContent(/Цены и суммы рассчитываются сервером/);
    expect(disclosure).toHaveTextContent(/До публичного запуска/);
    expect(disclosure).not.toHaveTextContent('Groq');
    expect(document.querySelector('meta[name="robots"]')).toHaveAttribute('content', 'noindex,follow');
  });

  it('makes the document discoverable through the public footer on public pages', () => {
    head();
    render(<PrivacyPage/>);
    expect(screen.getByRole('link', { name: 'Политика конфиденциальности' }))
      .toHaveAttribute('href', '/privacy');
    expect(screen.getByRole('link', { name: 'На главную' })).toHaveAttribute('href', '/');
  });
});
