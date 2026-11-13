import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { demoApi, resetDemoCatalog } from './demo/api';

const csv = readFileSync(resolve(process.cwd(),
  'public/templates/kpdesk-portfolio-synthetic-v1.csv'), 'utf8');

beforeEach(() => resetDemoCatalog());

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

describe('isolated demo reuses the existing workspace components', () => {
  it('activates a real bundled synthetic CSV without any protected API requests', async () => {
    window.history.replaceState({}, '', '/demo/prices/import');
    document.head.innerHTML = '<meta name="description"><meta name="robots">';
    const fetch = vi.fn(async (url: string) => {
      if (url !== '/templates/kpdesk-portfolio-synthetic-v1.csv') {
        throw Error('Unexpected demo request to ' + url);
      }
      return { ok: true, text: async () => csv };
    });
    vi.stubGlobal('fetch', fetch);
    render(<App/>);
    fireEvent.click(screen.getByRole('button', { name: 'Активировать учебный прайс' }));
    await waitFor(() => expect(window.location.pathname).toBe('/demo/prices'));
    expect(await screen.findByText('PAPER-A4-80')).toBeInTheDocument();
    expect(screen.getAllByRole('status').some(element =>
      element.textContent?.includes('Прайс из пяти товаров готов'))).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Продолжить с учебной заявкой' }));
    fireEvent.click(screen.getByRole('button', { name: /Распознать заявку/ }));
    await screen.findAllByRole('textbox', { name: 'Наименование' });
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары и рассчитать КП' }));
    await waitFor(() => expect(window.location.pathname).toBe('/demo/quotes/review'));
    expect(screen.getByText(/удалите позицию 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Предпросмотр КП' }));
    expect(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'К проверке' }));
    fireEvent.click(screen.getByRole('button', { name: 'Уточнить заявку' }));
    fireEvent.click(within(screen.getByRole('region', { name: 'Позиция 3' })).getByRole('button', { name: 'Удалить' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Условия подбора и расчёта' }), {
      target: { value: 'Наценка 18%. Бюджет до 1100 рублей.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить условия' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить условия' }));
    fireEvent.click(screen.getByRole('button', { name: 'Подобрать товары и рассчитать КП' }));
    await waitFor(() => expect(window.location.pathname).toBe('/demo/quotes/review'));
    fireEvent.click(screen.getByRole('button', { name: 'Предпросмотр КП' }));
    const sheet = screen.getByRole('article');
    expect(sheet).toHaveTextContent('ДЕМО — НЕ ЯВЛЯЕТСЯ КП');
    expect(sheet).toHaveTextContent('1 030,14 ₽');
    expect(screen.getAllByRole('button', { name: /Печать \/ сохранить PDF/ })[0]).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Начать демо заново' }));
    await waitFor(() => expect(window.location.pathname).toBe('/demo'));
    expect(screen.getByText('Не загружен')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('clears example extraction on source change and never calls a live LLM', async () => {
    window.history.replaceState({}, '', '/demo/requests/new');
    document.head.innerHTML = '<meta name="description"><meta name="robots">';
    const fetch = vi.fn(async () => { throw Error('Unexpected network call'); });
    vi.stubGlobal('fetch', fetch);
    render(<App/>);
    expect(screen.getByText(/Разбор и текст подготовлены заранее, ИИ здесь не вызывается/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Распознать заявку/ }));
    await waitFor(() =>
      expect(screen.getAllByRole('textbox', { name: 'Наименование' })).toHaveLength(3));
    fireEvent.change(screen.getByRole('textbox', { name: /Текст заявки/ }), {
      target: { value: 'Нужна бумага А4' },
    });
    expect(screen.queryByRole('textbox', { name: 'Наименование' })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('walks through interface targets with Next alone and restores existing work on close', async () => {
    window.history.replaceState({}, '', '/demo/requests/new');
    const fetch = vi.fn(async (url: string) => {
      if (url !== '/templates/kpdesk-portfolio-synthetic-v1.csv') throw Error('Unexpected demo request');
      return { ok: true, text: async () => csv };
    });
    vi.stubGlobal('fetch', fetch);
    render(<App/>);
    fireEvent.change(screen.getByRole('textbox', { name: /Текст заявки/ }), { target: { value: 'Моя незаконченная заявка' } });
    const start = screen.getByRole('button', { name: 'Показать инструкцию' });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const tour = await screen.findByRole('region', { name: 'Пошаговое знакомство с демо' });
    expect(tour).toHaveTextContent('Шаг 1 из 15');
    expect(within(tour).getByRole('link', { name: 'Выйти из демо' })).toHaveAttribute('href', '/');
    expect(within(tour).getByRole('button', { name: 'Назад' })).toBeDisabled();
    for (let step = 2; step <= 15; step++) {
      const next = within(tour).getByRole('button', { name: 'Далее' });
      expect(next).toBeEnabled();
      fireEvent.click(next);
      expect(tour).toHaveTextContent(`Шаг ${step} из 15`);
      await waitFor(() => expect(document.querySelector('[data-demo-spotlight]')).not.toBeNull());
      if (step === 7) {
        expect(screen.getAllByRole('textbox', { name: 'Наименование' })).toHaveLength(3);
        fireEvent.click(screen.getByRole('link', { name: 'Прайс-лист' }));
        expect(document.querySelector('[data-demo-spotlight]')).toBeNull();
        fireEvent.click(within(tour).getByRole('button', { name: 'Открыть этот шаг' }));
        fireEvent.click(within(tour).getByRole('button', { name: 'Назад' }));
        expect(tour).toHaveTextContent('Шаг 6 из 15');
        fireEvent.click(within(tour).getByRole('button', { name: 'Далее' }));
      }
      if (step === 11) expect(screen.getByText('Есть позиции, которые нужно уточнить')).toBeInTheDocument();
      if (step === 13) expect(screen.getByRole('article')).toHaveTextContent('ДЕМО — НЕ ЯВЛЯЕТСЯ КП');
    }
    fireEvent.click(within(tour).getByRole('button', { name: 'Завершить подсказки' }));
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
    expect(window.location.pathname).toBe('/demo/requests/new');
    expect(screen.getByRole('textbox', { name: /Текст заявки/ })).toHaveValue('Моя незаконченная заявка');
    expect(screen.queryByRole('textbox', { name: 'Наименование' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Показать инструкцию' })).toHaveFocus());
    fireEvent.click(screen.getByRole('button', { name: 'Показать инструкцию' }));
    const reopenedTour = await screen.findByRole('region', { name: 'Пошаговое знакомство с демо' });
    await waitFor(() => expect(reopenedTour).toHaveFocus());
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: /Текст заявки/ })).toHaveValue('Моя незаконченная заявка');
    expect(document.querySelector('[data-demo-spotlight]')).toBeNull();
    expect(fetch.mock.calls.every(([url]) => url === '/templates/kpdesk-portfolio-synthetic-v1.csv')).toBe(true);
    expect(fetch.mock.calls.length).toBeLessThanOrEqual(1);
  });
  it.each(['/demo', '/demo/'])('automatically highlights the first tutorial step on entry at %s and stays closed after dismissal', async (path) => {
    window.history.replaceState({}, '', path);
    const fetch = vi.fn(async (url: string) => {
      if (url !== '/templates/kpdesk-portfolio-synthetic-v1.csv') throw Error('Unexpected demo request');
      return { ok: true, text: async () => csv };
    });
    vi.stubGlobal('fetch', fetch);
    render(<App/>);
    expect(screen.queryByRole('button', { name: 'Начать с учебного прайса' })).not.toBeInTheDocument();
    const tour = await screen.findByRole('region', { name: 'Пошаговое знакомство с демо' });
    expect(tour).toHaveTextContent('Шаг 1 из 15');
    expect(document.querySelector('[data-demo-target="activate"]')).toHaveAttribute('data-demo-spotlight', 'true');
    expect(within(screen.getByRole('banner')).getByRole('link', { name: 'На главную' })).toHaveAttribute('href', '/');
    fireEvent.click(within(tour).getByRole('button', { name: 'Закрыть подсказки' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Показать инструкцию' })).toHaveFocus());
    expect(window.location.pathname).toBe('/demo');
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Показать инструкцию' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('link', { name: 'Создать КП' }));
    fireEvent.click(screen.getByRole('link', { name: 'Обзор демо' }));
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
    expect(fetch.mock.calls.every(([url]) => url === '/templates/kpdesk-portfolio-synthetic-v1.csv')).toBe(true);
  });

  it.each(['/demo/prices', '/demo/prices/import', '/demo/requests/new', '/demo/quotes/review', '/demo/quotes/preview', '/demo/unknown'])('provides a public-home exit while preserving manual route %s', async (path) => {
    window.history.replaceState({}, '', path);
    const fetch = vi.fn(async () => { throw Error('Unexpected network call'); });
    vi.stubGlobal('fetch', fetch);
    render(<App/>);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Показать инструкцию' })).toBeEnabled());
    const exit = within(screen.getByRole('banner')).getByRole('link', { name: 'На главную' });
    expect(exit).toHaveAttribute('href', '/');
    expect(exit).toHaveClass('demo-exit-link');
    expect(window.location.pathname).toBe(path);
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('does not redirect a user who navigates while the automatic tutorial is preparing', async () => {
    window.history.replaceState({}, '', '/demo');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => csv })));
    const originalExtract = demoApi.extract;
    let finishExtraction!: () => void;
    const pending = new Promise<void>(resolve => { finishExtraction = resolve; });
    const extraction = vi.spyOn(demoApi, 'extract').mockImplementation(async (...args) => {
      await pending;
      return originalExtract(...args);
    });
    render(<App/>);
    await waitFor(() => expect(extraction).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('link', { name: 'Создать КП' }));
    fireEvent.change(screen.getByRole('textbox', { name: /Текст заявки/ }), { target: { value: 'Моя новая заявка' } });
    await act(async () => { finishExtraction(); await pending; });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Показать инструкцию' })).toBeEnabled());
    expect(window.location.pathname).toBe('/demo/requests/new');
    expect(screen.getByRole('textbox', { name: /Текст заявки/ })).toHaveValue('Моя новая заявка');
    expect(screen.queryByRole('region', { name: 'Пошаговое знакомство с демо' })).not.toBeInTheDocument();
  });

  it('discards tutorial preparation after unmount', async () => {
    window.history.replaceState({}, '', '/demo');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => csv })));
    const originalExtract = demoApi.extract;
    let finishExtraction!: () => void;
    const pending = new Promise<void>(resolve => { finishExtraction = resolve; });
    const extraction = vi.spyOn(demoApi, 'extract').mockImplementation(async (...args) => {
      await pending;
      return originalExtract(...args);
    });
    const { unmount } = render(<App/>);
    await waitFor(() => expect(extraction).toHaveBeenCalled());
    unmount();
    window.history.replaceState({}, '', '/');
    await act(async () => { finishExtraction(); await pending; });
    expect(window.location.pathname).toBe('/');
    expect(document.querySelector('[data-demo-spotlight]')).toBeNull();
  });

});
