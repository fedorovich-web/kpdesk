import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PublicPricingCards } from './PublicPricingCards';
import { PricingPage } from './PricingPage';
import { PublicHome } from './PublicHome';

describe('researched launch pricing shared across public pages', () => {
  it('exposes a named group for tariff cards to assistive technology', () => {
    render(<PublicPricingCards/>);
    expect(screen.getByRole('group', { name: 'Планируемые тарифы KPDesk' }))
      .toContainElement(screen.getByRole('heading', { name: 'Старт' }));
  });
  it('shows the three monthly organization prices with scope and planned capacities', () => {
    render(<PublicPricingCards/>);
    for (const [name, price, people, quotes, catalog] of [
      ['Старт', '299 ₽', '1 сотрудник', '30 новых КП в месяц', '500 товаров в каталоге'],
      ['Команда', '499 ₽', 'До 5 сотрудников', '100 новых КП в месяц', '2 000 товаров в каталоге'],
      ['Бизнес', '990 ₽', 'До 15 сотрудников', '300 новых КП в месяц', '5 000 товаров в каталоге'],
    ]) {
      const card=screen.getByRole('heading',{name:name!}).closest('article')!;
      for(const text of [price,people,quotes,catalog]) expect(within(card).getByText(text!)).toBeInTheDocument();
      expect(card).toHaveTextContent('за организацию');
      expect(within(card).getByRole('link', {name:'Начать работу'})).toHaveAttribute('href','/app');
    }
    expect(screen.queryByText('По запросу')).not.toBeInTheDocument();
  });

  it.each([PricingPage, PublicHome])('keeps actual price cards without the rejected launch notice on both pages', Page => {
    render(<Page/>);
    for(const price of ['299 ₽','499 ₽','990 ₽']) expect(screen.getAllByText(price).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Планируемые условия запуска|Приём оплаты и включение тарифов готовятся/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button',{name:/оплатить|купить/i})).not.toBeInTheDocument();
  });

  it('explains future quota counting and separates it from current AI limits', () => {
    render(<PricingPage/>);
    expect(screen.getAllByText(/Редакции и повторное скачивание PDF не расходуют лимит новых КП/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Тариф не означает безлимитные обращения к ИИ/)).toBeInTheDocument();
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/CRM|выделенный менеджер|скидка 20%/i);
  });

  it('does not promise client links as an included entitlement or deny the conditional feature', () => {
    render(<PricingPage/>);
    expect(screen.getByText(/Доступ по ссылке зависит от подключения этой функции в кабинете/)).toBeInTheDocument();
    expect(screen.getByText(/Прямые CRM-подключения пока не включены/)).toBeInTheDocument();
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/ссылк|отправка|аналитика просмотров/i);
    expect(screen.queryByText(/отправка клиентских ссылок пока не включен/i)).not.toBeInTheDocument();
  });
});
