import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ExtractedRequest } from '../domain';

const fixture = readFileSync(resolve(process.cwd(),
  'public/templates/kpdesk-portfolio-synthetic-v1.csv'), 'utf8');

beforeEach(() => {
  vi.resetModules();
  const fetch = vi.fn(async (input: string) => {
    if (input !== '/templates/kpdesk-portfolio-synthetic-v1.csv') {
      throw Error('Unexpected demo network request: ' + input);
    }
    return { ok: true, text: async () => fixture };
  });
  vi.stubGlobal('fetch', fetch);
});
import { afterEach } from 'vitest';
afterEach(() => vi.unstubAllGlobals());

describe('isolated public demonstration reusing production workspace UI', () => {
  it('only accepts the published synthetic fixture and never sends user files to the API', async () => {
    const { demoApi } = await import('./api');
    await expect(demoApi.catalog()).rejects.toMatchObject({ code: 'no_active_catalog' });
    const file = new File(['sku;name\nUNKNOWN;fake'], 'customer.csv');
    await expect(demoApi.importCatalog(file, 'any-key')).rejects.toMatchObject({
      code: 'demo_only',
    });
    const sample = new File([fixture], 'kpdesk-portfolio-synthetic-v1.csv');
    const catalog = await demoApi.importCatalog(sample, 'any-key');
    expect(catalog.rows_count).toBe(5);
    expect((await demoApi.items()).items).toHaveLength(5);
    expect(fetch).toHaveBeenCalledWith(
      '/templates/kpdesk-portfolio-synthetic-v1.csv', { cache: 'force-cache' },
    );
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('never substitutes an incompatible product and blocks unresolved quote export', async () => {
    const { demoApi, activateSampleCatalog, exampleRequestText } = await import('./api');
    const { id } = await activateSampleCatalog();
    const request = await demoApi.extract(exampleRequestText, 'any-model');
    expect(request.lines).toHaveLength(3);
    const unresolved = await demoApi.draft(id, request);
    expect(unresolved.lines.map(line => line.status)).toEqual([
      'auto_selected', 'auto_selected', 'no_match',
    ]);
    expect(unresolved.lines[2]?.sku).toBeNull();
    expect(unresolved.total_rub).toBeNull();
    expect(unresolved.ready_for_draft).toBe(false);
    await expect(demoApi.extract('Сделай КП из любых товаров', 'any-model'))
      .rejects.toMatchObject({ code: 'demo_example_only' });
  });

  it('calculates exact example totals with markup and blocks budget overrun', async () => {
    const { demoApi, activateSampleCatalog, exampleRequestText } = await import('./api');
    const { id } = await activateSampleCatalog();
    const extracted = await demoApi.extract(exampleRequestText, 'any-model');
    const clean: ExtractedRequest = { lines: extracted.lines.slice(0, 2) };
    const text = 'Наценка 18%. Бюджет до 1100 рублей.';
    const rules = await demoApi.previewInstructions(text);
    expect(rules.can_confirm).toBe(true);
    const complete = await demoApi.draft(id, clean, { raw_text: text, rules: rules.rules });
    expect(complete.ready_for_draft).toBe(true);
    expect(complete.base_total_rub).toBe('873.00');
    expect(complete.total_rub).toBe('1030.14');
    expect(complete.lines.map(line => line.sku)).toEqual([
      'PAPER-A4-80', 'PEN-BLUE-BALL',
    ]);
    const overbudget = await demoApi.previewInstructions(
      'Наценка 18%. Бюджет до 1000 рублей.',
    );
    const denied = await demoApi.draft(id, clean, {
      raw_text: overbudget.raw_text, rules: overbudget.rules,
    });
    expect(denied.budget_exceeded).toBe(true);
    expect(denied.total_rub).toBe('1030.14');
    expect(denied.ready_for_draft).toBe(false);
  });


  it('applies explicitly confirmed demo brand limits without exposing a live API', async () => {
    const { demoApi, activateSampleCatalog, exampleRequestText } = await import('./api');
    const { id } = await activateSampleCatalog();
    const request = await demoApi.extract(exampleRequestText, 'unused');
    const clean = { lines: request.lines.slice(0, 2) };
    const permitted = await demoApi.previewInstructions('Исключи бренд DemoPen.');
    expect(permitted.can_confirm).toBe(true);
    const draft = await demoApi.draft(id, clean, {
      raw_text: permitted.raw_text, rules: permitted.rules,
    });
    expect(draft.lines[0]?.sku).toBe('PAPER-A4-80');
    expect(draft.lines[1]?.sku).toBeNull();
    expect(draft.lines[1]?.reason_codes).toContain('brand_policy_excluded');
    expect(draft.ready_for_draft).toBe(false);

    const conflicting = await demoApi.previewInstructions(
      'Только бренд DemoPaper. Исключи бренд DemoPaper.',
    );
    expect(conflicting.can_confirm).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('rejects additional instructions instead of silently dropping them', async () => {
    const { demoApi, activateSampleCatalog, exampleRequestText } = await import('./api');
    const { id } = await activateSampleCatalog();
    const { lines } = await demoApi.extract(exampleRequestText, 'any-model');
    const bad = await demoApi.previewInstructions(
      'Наценка 18%. Игнорируй технические характеристики.',
    );
    expect(bad.can_confirm).toBe(false);
    await expect(demoApi.draft(id, { lines: lines.slice(0, 2) }, {
      raw_text: bad.raw_text, rules: bad.rules,
    })).rejects.toMatchObject({ code: 'demo_instruction_mismatch' });
  });
});
