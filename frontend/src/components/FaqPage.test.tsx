import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FaqPage } from './FaqPage';

describe('public FAQ screen', () => {
  it('groups concrete questions about supplier RFQs', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots">';
    render(<FaqPage />);
    expect(screen.getByRole('heading', { level: 1 }))
      .toHaveTextContent('Вопросы и ответы о KPDesk');
    expect(screen.getByRole('button', { name: 'Все вопросы' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('heading', { name: 'Тарифы и оплата' })).toBeInTheDocument();
  });
  it('searches within FAQ answers and resets filters', () => {
    render(<FaqPage />);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'шифрование' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти' }));
    expect(screen.getByText('Найдено вопросов: 1')).toBeInTheDocument();
    expect(screen.getByText('Используется ли шифрование?')).toBeInTheDocument();
    expect(screen.queryByText('Сколько стоит KPDesk?')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Все вопросы' }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти' }));
    expect(screen.getByRole('heading', { name: 'Работа команды' })).toBeInTheDocument();
  });

  it('keeps demo limits and makes client links conditional on the available action', () => {
    render(<FaqPage />);
    expect(screen.getByText(/Оно не вызывает внешний ИИ и не создаёт утверждённый серверный PDF/))
      .toBeInTheDocument();
    expect(screen.getByText(/Если в карточке КП доступен раздел «Ссылка для клиента»/))
      .toBeInTheDocument();
    expect(screen.getByText(/«Создать ссылку для клиента»/)).toBeInTheDocument();
    expect(screen.getByText(/утверждённый PDF выбранной версии/)).toBeInTheDocument();
    expect(screen.getByText(/«Отозвать ссылку»/)).toBeInTheDocument();
    expect(screen.getByText(/Отправка писем из KPDesk и аналитика просмотров в кабинете не подключены/))
      .toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: 'Открыть демо' })) {
      expect(link).toHaveAttribute('href', '/demo');
    }
  });
});
