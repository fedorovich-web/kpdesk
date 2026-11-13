import { existsSync, readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { designAssets } from './design-assets';

describe('approved design asset references', () => {
  it('ships the same original default logo for browser previews and PDF', () => {
    const browser = resolve(process.cwd(), 'public/assets/kpdesk-default-v1.png');
    const pdf = resolve(process.cwd(), '../backend/src/kpdesk/assets/branding/kpdesk-default-v1.png');
    expect(existsSync(browser), 'Default logo URL used by company and proposal previews must resolve').toBe(true);
    expect(readFileSync(browser)).toEqual(readFileSync(pdf));
  });
  it('ships every asset referenced by a UI component', () => {
    const values = [designAssets.logo, designAssets.publicHero, designAssets.dashboardHero,
      designAssets.offerStack, designAssets.faqHero, designAssets.featuresHero,
      designAssets.processHero, designAssets.integrationsHero, designAssets.integrationScheme,
      designAssets.pricingHero, designAssets.benefitsHero, designAssets.notFoundHero, ...Object.values(designAssets.icon)];
    for (const url of values) {
      expect(url).toMatch(/^\/assets\/[\w./-]+\.avif$/);
      const pathname = resolve(process.cwd(), 'public', url.slice(1));
      expect(existsSync(pathname), url + ' is missing').toBe(true);
      expect(statSync(pathname).size, url + ' is empty').toBeGreaterThan(1000);
    }
    expect(new Set(values).size).toBe(values.length);
  });
});
