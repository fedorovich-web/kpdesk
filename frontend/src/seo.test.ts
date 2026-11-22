import { describe, expect, it, vi } from 'vitest';
import { applyDocumentSeo, metadata, publicSeo, publicSections } from './seo';

describe('verified B2B RFQ search metadata', () => {
  it('removes canonical elements from private pages instead of leaving invalid empty links', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots"><link rel="canonical" href="https://kpdesk.ru/">';
    applyDocumentSeo('app');
    expect(document.querySelector('link[rel="canonical"]')).toBeNull();
  });
  it('creates a valid public canonical after returning from a private document', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots">';
    vi.stubGlobal('window', { location: { protocol: 'https:', hostname: 'kpdesk.ru' } });
    try {
      applyDocumentSeo('pricing');
      expect(document.querySelector('link[rel="canonical"]')).toHaveAttribute('href', 'https://kpdesk.ru/pricing');
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('names the supplier task rather than unsupported performance claims', () => {
    expect(publicSeo.title).toMatch(/ИИ-генератор коммерческих предложений из прайса/);
    expect(publicSeo.description).toMatch(/прайс/);
    expect(publicSeo.description).toMatch(/скачайте PDF/);
    expect(publicSeo.description).not.toMatch(/запланирован|в разработке|следующий этап/i);
    expect(publicSeo.h1).toMatch(/Коммерческие предложения по заявке и вашему прайсу/);
    expect(publicSeo.lead).toContain('прайс');
    expect(publicSeo.lead).toContain('заявку клиента');
    expect(publicSeo.description).not.toMatch(/гарант|конверси|в \d+ раз|99,\d+%/i);
    expect(publicSections[0].items).toHaveLength(4);
  });
  it('isolates metadata and robots directives of public pages and workspaces', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots"><link rel="canonical">';
    applyDocumentSeo('public');
    expect(document.title).toBe(publicSeo.title);
    expect(document.querySelector('meta[name="robots"]')?.getAttribute('content'))
      .toBe('index,follow');
    applyDocumentSeo('pricing');
    expect(document.title).toBe(metadata.pricing.title);
    applyDocumentSeo('app', 'Прайс-лист');
    expect(document.title).toBe('Прайс-лист — KPDesk');
    expect(document.querySelector('meta[name="robots"]')?.getAttribute('content'))
      .toBe('noindex,nofollow');
    expect(document.querySelector('link[rel="canonical"]')).toBeNull();
  });
  it('does not turn file imports or the browser demo into unsupported services', () => {
    expect(metadata.integrations.description).toContain('выгрузку из 1С');
    expect(metadata.integrations.title).not.toMatch(/интеграции с 1С|API/);
    expect(metadata.demo.description).toContain('не вызывает внешний ИИ');
    expect(publicSeo.description).toContain('проверьте сумму');
    expect(publicSeo.lead).toContain('ИИ поможет разобрать требования');
    expect(metadata.features.description).toContain('сравните варианты');
    expect(metadata.features.description).toContain('текстом, подготовленным ИИ');
    expect(metadata.pricing.description).toMatch(/299 ₽.*499 ₽.*990 ₽.*за 30 дней для организации/);
    expect(metadata.pricing.description).not.toMatch(/пока не подключена|автопродлен/i);
  });
  it('keeps route titles and descriptions synchronized with Open Graph metadata', () => {
    document.head.innerHTML = '<meta name="description"><meta name="robots"><meta property="og:title"><meta property="og:description">';
    for (const route of ['public', 'features', 'how', 'integrations', 'pricing', 'faq'] as const) {
      applyDocumentSeo(route);
      expect(document.title).toBe(metadata[route].title);
      expect(document.querySelector('meta[property="og:title"]')?.getAttribute('content')).toBe(metadata[route].title);
      expect(document.querySelector('meta[property="og:description"]')?.getAttribute('content')).toBe(metadata[route].description);
    }
  });
});
