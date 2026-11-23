import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FeaturesPage } from './FeaturesPage';
import { HowItWorksPage } from './HowItWorksPage';
import { IntegrationsPage } from './IntegrationsPage';
import { PricingPage } from './PricingPage';

function setupHead() {
  document.head.innerHTML = [
    '<meta name="description">',
    '<meta name="robots">',
    '<meta property="og:title">',
    '<meta property="og:description">',
  ].join('');
}

describe('public feature and pricing pages', () => {
  it('describes supported features and links to the relevant public instructions', () => {
    setupHead();
    render(<FeaturesPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(document.title).toBe('ИИ для создания КП, подбор товаров и расчёт — KPDesk');
    expect(screen.getByRole('heading', { name: 'Что умеет KPDesk при подготовке КП' })).toBeInTheDocument();
    expect(screen.getByText(/Добавьте сотрудников и назначьте им права на работу с прайсами/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'ИИ пишет текст КП' })).toBeInTheDocument();
    expect(screen.getByText(/KPDesk предложит позиции из каталога по требованиям заявки/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Посмотреть этапы работы' })).toHaveAttribute('href', '/how-it-works#process');
    expect(screen.getByRole('link', { name: 'Что должно быть в прайсе' })).toHaveAttribute('href', '/integrations#api');
    expect(screen.getByRole('link', { name: 'Попробовать подбор' })).toHaveAttribute('href', '/demo');
    expect(screen.queryByText('В разработке')).not.toBeInTheDocument();
  });
  it('shows actual five steps rather than unverified automatic delivery', () => {
    setupHead();
    render(<HowItWorksPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Пять шагов от загрузки прайса до готового PDF' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Загрузите прайс' })).toHaveLength(1);
    expect(document.querySelectorAll('.marketing-step-details > li')).toHaveLength(5);
    expect(screen.getByText(/KPDesk предложит товары из каталога. Сравните их с заявкой клиента и подтвердите выбор/)).toBeInTheDocument();
    expect(screen.getByText(/ИИ подготовит текст по этим данным и выбранным товарам/)).toBeInTheDocument();
  });
  it('describes 1С and CRM as file imports and lists required catalog columns', () => {
    setupHead();
    render(<IntegrationsPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Excel' })).toBeInTheDocument();
    expect(screen.getByText('Импорт файла')).toBeInTheDocument();
    for (const heading of ['Битрикс24', 'amoCRM']) {
      const card = screen.getByRole('heading', { name: heading }).closest('article')!;
      expect(within(card).getByText('Через CSV или XLSX')).toBeInTheDocument();
    }
    const columns = screen.getByRole('heading', { name: 'Четыре обязательных столбца прайса' }).closest('section');
    expect(columns).toHaveTextContent('sku, name, price_rub и sale_unit');
    expect(screen.queryByText(/Service Account|сервисный аккаунт|Контрактные тесты адаптеров/))
      .not.toBeInTheDocument();
    expect(document.head.querySelector('meta[name="description"]')).toHaveAttribute('content', expect.stringContaining('выгрузку из 1С'));
    expect(document.title).not.toMatch(/интеграции с 1С/);
  });
  it('shows the 30-day prices, comparison and payment instructions for the organization', () => {
    setupHead();
    render(<PricingPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Три тарифа для команд разного размера' }))
      .toBeInTheDocument();
    for (const price of ['299 ₽', '499 ₽', '990 ₽']) expect(screen.getAllByText(price).length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: 'Сравнение тарифов KPDesk' })).toBeInTheDocument();
    expect(screen.getByText(/оплачивает счёт через Lava.top. Оплата разовая, без автосписаний/)).toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: 'Выбрать в кабинете' })) {
      expect(link).toHaveAttribute('href', '/app');
    }
    expect(screen.queryByRole('button', { name: /ежемесячно|ежегодно|оплатить/i }))
      .not.toBeInTheDocument();
    expect(screen.queryByText(/Планируемые условия запуска|Приём оплаты и включение тарифов готовятся/i))
      .not.toBeInTheDocument();
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/дешевле всех|безлимитный ИИ|SLA|поддержка 24\/7/i);
  });
});
