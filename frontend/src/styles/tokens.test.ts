import { readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = process.cwd();
const styles = [
  'src/styles.css',
  'src/styles/public.css',
  'src/styles/faq.css',
  'src/styles/marketing.css',
  'src/styles/not-found.css',
  'src/styles/catalog.css',
  'src/styles/review.css',
  'src/styles/request.css',
  'src/styles/preview.css',
  'src/styles/mobile-navigation.css',
].map((path) => readFileSync(resolve(root, path), 'utf8')).join('\n');
const tokens = readFileSync(resolve(root, 'src/styles/tokens.css'), 'utf8');
const faces = readFileSync(resolve(root, 'src/styles/fonts.css'), 'utf8');

describe('self-hosted KPDesk design system', () => {
  it('keeps ordinary text and button labels at WCAG AA contrast on their actual surfaces', () => {
    const colors = (block: string): Record<string, string> => Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*(#[\da-f]{6})\s*;/gi)]
      .map(match => [match[1], match[2]]));
    const base = colors(tokens.match(/:root\s*\{([^}]+)\}/)![1]!);
    const publicColors = { ...base, ...colors(tokens.match(/\.public-page\s*\{([^}]+)\}/)![1]!) };
    const luminance = (hex: string) => {
      const channels = [1, 3, 5].map(offset => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255)
        .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return channels[0]! * .2126 + channels[1]! * .7152 + channels[2]! * .0722;
    };
    const contrast = (a: string, b: string) => {
      const [low, high] = [luminance(a), luminance(b)].sort((x, y) => x - y);
      return (high! + .05) / (low! + .05);
    };
    for (const palette of [base, publicColors]) {
      for (const text of ['--color-muted', '--color-muted-alt', '--color-text-mid', '--color-accent-strong']) {
        for (const background of ['--color-surface', '--color-surface-alt', '--color-accent-pale', '--color-warning-surface']) {
          expect(contrast(palette[text]!, palette[background]!), text + ' on ' + background).toBeGreaterThanOrEqual(4.5);
        }
      }
      const foreground = styles.match(/\.button-primary\s*\{[^}]*?\bcolor:\s*var\((--[\w-]+)\)/)![1]!;
      for (const state of ['--gradient-button', '--gradient-button-hover']) {
        const gradient = tokens.match(new RegExp(state + ':\\s*([^;]+);'))![1]!;
        for (const background of gradient.match(/#[\da-f]{6}/gi)!) {
          expect(contrast(palette[foreground]!, background), 'Button label on ' + state).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it('enforces at least 14 px UI text, 12 px only for breadcrumbs', () => {
    const sizes = [...tokens.matchAll(/(--text-[\w-]+):\s*(\d+(?:\.\d+)?)px/g)]
      .map((match) => ({ name: match[1], pixels: Number(match[2]) }));
    expect(sizes.find((item) => item.name === '--text-base')?.pixels).toBe(14);
    expect(sizes.find((item) => item.name === '--text-breadcrumb')?.pixels).toBe(12);
    expect(sizes.filter((item) => item.pixels < 14).map((item) => item.name))
      .toEqual(['--text-breadcrumb']);
    expect(styles).toContain('.breadcrumbs { font-size: var(--text-breadcrumb);');
    expect(styles).not.toContain('var(--text-xs)');
  });

  it('defines every CSS custom property consumed by the UI', () => {
    const declarations = new Set(
      [...tokens.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]),
    );
    const usages = new Set(
      [...(tokens + styles).matchAll(/var\((--[\w-]+)/g)].map((match) => match[1]),
    );
    expect([...usages].filter((name) => name && !declarations.has(name))).toEqual([]);
    expect([...declarations].filter((name) => name && !usages.has(name))).toEqual([]);
  });

  it('keeps UI colors, typography and interactive gradients in one token sheet', () => {
    expect(styles).not.toMatch(/#[\da-fA-F]{3,8}\b/);
    expect(styles).not.toMatch(/\bfont-size:\s*\d+(?:\.\d+)?px/);
    expect(styles).not.toMatch(/linear-gradient\(/);
    expect(styles).toContain('var(--gradient-button-hover)');
    expect(styles).toContain('var(--transition-interactive)');
    expect(styles).toContain('var(--button-padding-inline)');
    for (const size of ['h1', 'h2', 'h3', 'h4']) {
      expect(tokens).toContain('--text-' + size + ':');
      expect(styles).toContain('font-size: var(--text-' + size + ')');
    }
  });

  it('loads local variable Cyrillic, Latin and ruble-symbol glyph subsets', () => {
    expect(styles).not.toContain('fonts.googleapis.com');
    expect(faces).not.toMatch(/https?:\/\//);
    expect(faces.match(/@font-face/g)).toHaveLength(3);
    expect(faces).toContain('font-weight: 100 900');
    expect(faces).toContain('U+20BD');
    for (const script of ['cyrillic', 'latin', 'ruble']) {
      const bytes = statSync(resolve(root, 'public/fonts/inter/inter-' + script + '-wght-normal.woff2')).size;
      expect(bytes).toBeGreaterThan(script === 'ruble' ? 1_000 : 10_000);
      expect(faces).toContain('inter-' + script + '-wght-normal.woff2');
    }
    const license = readFileSync(resolve(root, 'public/fonts/inter/OFL.txt'), 'utf8');
    expect(license).toContain('SIL OPEN FONT LICENSE');
  });
});
