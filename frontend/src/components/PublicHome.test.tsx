import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PublicHome } from './PublicHome';

describe('public landing for B2B suppliers', () => {
  it('places complete pricing and expanded FAQ in separate consecutive sections', () => {
    render(<PublicHome />);
    const pricing = screen.getByRole('region', { name: 'Тарифы на 30 дней для вашей команды' });
    const faq = screen.getByRole('region', { name: 'Частые вопросы о прайсах и подготовке КП' });
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
      .toHaveTextContent('Создавайте коммерческие предложения с ИИ');
    expect(within(screen.getByRole('region', { name: 'Создавайте коммерческие предложения с ИИ' }))
      .getByText(/Вставьте заявку клиента и загрузите прайс/))
      .toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Разбор заявки, подбор товаров и расчёт КП' }))
      .toBeInTheDocument();
    expect(screen.getByText(/Тарифы на 30 дней для вашей команды/))
      .toBeInTheDocument();
    for (const price of ['299 ₽', '499 ₽', '990 ₽']) expect(screen.getByText(price)).toBeInTheDocument();
  });

  it('keeps FAQ answers readable in semantic details without JS interaction', () => {
    render(<PublicHome />);
    const summary = screen.getByText('Как создать коммерческое предложение по заявке?');
    expect(summary.tagName).toBe('SUMMARY');
    const container = summary.closest('details');
    expect(container).not.toBeNull();
    expect(container).toHaveAttribute('name', 'kpdesk-home-faq');
    expect(container).toHaveTextContent(/Проверьте требования, которые выделил ИИ/);
    expect(container).toHaveTextContent(/утвердите КП/);
  });

  it('offers clear actions for a real quote and the guided demo', () => {
    render(<PublicHome />);
    expect(screen.getByRole('heading', { name: 'Генерация текста КП' })).toBeInTheDocument();
    expect(screen.queryByText(/Для своих прайсов, генерации текста и сохранения КП нужен настроенный кабинет компании/))
      .not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Соберите КП по своей заявке' })).toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: 'Открыть кабинет' })) {
      expect(link).toHaveAttribute('href', '/app');
    }
    for (const link of screen.getAllByRole('link', { name: 'Попробовать демо' })) {
      expect(link).toHaveAttribute('href', '/demo');
    }
    for (const link of screen.getAllByRole('link', { name: 'Выбрать в кабинете' })) {
      expect(link).toHaveAttribute('href', '/app');
    }
    expect(screen.queryByText('PDF-черновик КП')).not.toBeInTheDocument();
  });
});
