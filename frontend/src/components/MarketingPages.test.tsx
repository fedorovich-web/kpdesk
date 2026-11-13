import { render, screen } from '@testing-library/react';
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
  it('separates verified features from integration and ML work in progress', () => {
    setupHead();
    render(<FeaturesPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(document.title).toMatch(/Автоматический подбор товаров и ИИ-генерация текста КП/);
    expect(screen.getByRole('heading', { name: 'Что KPDesk берёт на себя при подготовке КП' })).toBeInTheDocument();
    expect(screen.getByText(/Добавьте сотрудников и назначьте им права на работу с прайсами/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'ИИ пишет текст КП' })).toBeInTheDocument();
    expect(screen.getByText(/KPDesk предложит позиции из каталога по требованиям заявки/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Добавить заявку' })).toHaveAttribute('href', '/app#workspace-request');
    expect(screen.getByRole('link', { name: 'Загрузить прайс' })).toHaveAttribute('href', '/app#workspace-catalog');
    expect(screen.getByRole('link', { name: 'Открыть свои КП' })).toHaveAttribute('href', '/app#workspace-quotes');
    expect(screen.queryByText('В разработке')).not.toBeInTheDocument();
  });
  it('shows actual five steps rather than unverified automatic delivery', () => {
    setupHead();
    render(<HowItWorksPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Пять шагов от прайса до утверждённого КП' })).toBeInTheDocument();
    expect(document.querySelectorAll('.marketing-step-details > li')).toHaveLength(5);
    expect(screen.getByText(/KPDesk предложит товары из каталога. Сравните их с заявкой клиента и подтвердите выбор/)).toBeInTheDocument();
    expect(screen.getByText(/ИИ подготовит текст по этим данным и выбранным товарам/)).toBeInTheDocument();
  });
  it('keeps connected formats and CRM plans clearly distinguishable', () => {
    setupHead();
    render(<IntegrationsPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByText(/Подготовьте названия, артикулы, единицы, цены и характеристики в столбцах прайса KPDesk/))
      .toBeInTheDocument();
    expect(screen.getByText('Импорт файла')).toBeInTheDocument();
    expect(screen.getByText(/прямая синхронизация с этими системами пока не подключена/))
      .toBeInTheDocument();
    expect(screen.queryByText(/Service Account|сервисный аккаунт|Контрактные тесты адаптеров/))
      .not.toBeInTheDocument();
    expect(document.title).toMatch(/выгрузки из 1С/);
    expect(document.title).not.toMatch(/интеграции с 1С/);
  });
  it('shows the authorized launch prices and comparison without pretending checkout is connected', () => {
    setupHead();
    render(<PricingPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Выберите тариф для своей команды' }))
      .toBeInTheDocument();
    for (const price of ['299 ₽', '499 ₽', '990 ₽']) expect(screen.getAllByText(price).length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: 'Сравнение планируемых тарифов KPDesk' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /ежемесячно|ежегодно|оплатить/i }))
      .not.toBeInTheDocument();
    expect(screen.queryByText(/Планируемые условия запуска|Приём оплаты и включение тарифов готовятся/i))
      .not.toBeInTheDocument();
    expect(document.querySelector('.public-plan-grid')).not.toHaveTextContent(/дешевле всех|безлимитный ИИ|SLA|поддержка 24\/7/i);
  });
});
