import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PublicHome } from './PublicHome';

describe('public landing for B2B suppliers', () => {
  it('places complete pricing and expanded FAQ in separate consecutive sections', () => {
    render(<PublicHome />);
    const pricing = screen.getByRole('region', { name: 'Тарифы для вашей команды' });
    const faq = screen.getByRole('region', { name: 'Вопросы о работе с KPDesk' });
    expect(pricing.parentElement).toBe(faq.parentElement);
    expect(pricing.nextElementSibling).toBe(faq);
    expect(within(pricing).getAllByRole('article')).toHaveLength(3);
    const questions = faq.querySelectorAll('summary');
    expect(questions).toHaveLength(10);
    expect(new Set(Array.from(questions, item => item.textContent)).size).toBe(10);
    expect(within(faq).getByText('Что произойдёт с КП, если изменится прайс?')).toBeInTheDocument();
  });
  it('uses one relevant search heading and verified quote workflow', () => {
    document.head.innerHTML = '<meta name="description" content=""><meta name="robots" content="">';
    render(<PublicHome />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 1 }))
      .toHaveTextContent('ИИ-генератор коммерческих предложений с подбором товаров');
    expect(within(screen.getByRole('region', { name: 'ИИ-генератор коммерческих предложений с подбором товаров' }))
      .getByText(/Загрузите прайс и добавьте заявку клиента/))
      .toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'От заявки до текста КП и расчёта стоимости' }))
      .toBeInTheDocument();
    expect(screen.getByText(/Тарифы для вашей команды/))
      .toBeInTheDocument();
    for (const price of ['299 ₽', '499 ₽', '990 ₽']) expect(screen.getByText(price)).toBeInTheDocument();
  });

  it('keeps FAQ answers readable in semantic details without JS interaction', () => {
    render(<PublicHome />);
    const summary = screen.getByText('Как создать коммерческое предложение по заявке?');
    expect(summary.tagName).toBe('SUMMARY');
    const container = summary.closest('details');
    expect(container).not.toBeNull();
    expect(container).toHaveTextContent(/Проверьте товары, текст и условия/);
    expect(container).toHaveTextContent(/утвердите КП/);
  });

  it('describes text and approval while directing the CTA to the limited demo', () => {
    render(<PublicHome />);
    expect(screen.getByRole('heading', { name: 'Генерация текста КП' })).toBeInTheDocument();
    expect(screen.getByText(/Для своих прайсов, генерации текста и сохранения КП нужен настроенный кабинет компании/))
      .toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: 'Открыть демо' })) {
      expect(link).toHaveAttribute('href', '/demo');
    }
    expect(screen.getByRole('link', { name: 'Попробовать на примере' })).toHaveAttribute('href', '/demo');
    expect(screen.queryByText('PDF-черновик КП')).not.toBeInTheDocument();
  });
});
