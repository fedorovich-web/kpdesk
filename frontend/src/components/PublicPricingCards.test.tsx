import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PublicPricingCards } from './PublicPricingCards';
import { PricingPage } from './PricingPage';
import { PublicHome } from './PublicHome';

describe('researched launch pricing shared across public pages', () => {
  it('exposes a named group for tariff cards to assistive technology', () => {
    render(<PublicPricingCards/>);
    expect(screen.getByRole('group', { name: 'Тарифы KPDesk' }))
      .toContainElement(screen.getByRole('heading', { name: 'Старт' }));
  });
  it('shows all three 30-day organization prices and access limits', () => {
    render(<PublicPricingCards/>);
    for (const [name, price, people, quotes, catalog] of [
      ['Старт', '299 ₽', '1 сотрудник', '30 новых КП за период', '500 товаров в каталоге'],
      ['Команда', '499 ₽', 'До 5 сотрудников', '100 новых КП за период', '2 000 товаров в каталоге'],
      ['Бизнес', '990 ₽', 'До 15 сотрудников', '300 новых КП за период', '5 000 товаров в каталоге'],
    ]) {
      const card=screen.getByRole('heading',{name:name!}).closest('article')!;
      for(const text of [price,people,quotes,catalog]) expect(within(card).getByText(text!)).toBeInTheDocument();
      expect(card).toHaveTextContent('за организацию');
      expect(card).toHaveTextContent('за 30 дней');
      expect(within(card).getByRole('link', {name:'Выбрать в кабинете'})).toHaveAttribute('href','/app');
    }
    expect(screen.queryByText('По запросу')).not.toBeInTheDocument();
  });

  it.each([PricingPage, PublicHome])('keeps actual price cards without the rejected launch notice on both pages', Page => {
    render(<Page/>);
    for(const price of ['299 ₽','499 ₽','990 ₽']) expect(screen.getAllByText(price).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Планируемые условия запуска|Оплата пока не подключена/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button',{name:/оплатить|купить/i})).not.toBeInTheDocument();
  });

  it('explains quote usage and separates it from the AI quota', () => {
    render(<PricingPage/>);
    expect(screen.getAllByText(/Редакции и повторное скачивание PDF не расходуют лимит новых КП/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Для разбора заявки и генерации текста действует отдельный лимит обращений/)).toBeInTheDocument();
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/CRM|выделенный менеджер|скидка 20%/i);
  });

  it('explains one-time owner payment without promising additional entitlements', () => {
    render(<PricingPage/>);
    expect(screen.getByText(/Руководитель организации открывает «Тариф и оплата»/)).toHaveTextContent(/через Lava\.top.*без автосписаний/);
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/ссылк|отправка|аналитика просмотров/i);
  });
});
