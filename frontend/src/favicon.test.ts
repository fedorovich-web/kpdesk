import { readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = process.cwd();
const inPublic = (name: string) => resolve(root, 'public', name);
const html = readFileSync(resolve(root, 'index.html'), 'utf8');

const pngDimensions = (name: string): [number, number] => {
  const buffer = readFileSync(inPublic(name));
  expect(buffer.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
  return [buffer.readUInt32BE(16), buffer.readUInt32BE(20)];
};

describe('approved KPDesk favicon package', () => {
  it('includes all icons and links them from the document head', () => {
    const references = [
      'favicon.svg',
      'favicon-96x96.png',
      'favicon.ico',
      'apple-touch-icon.png',
      'site.webmanifest',
    ];
    for (const file of references) {
      expect(statSync(inPublic(file)).size).toBeGreaterThan(400);
      expect(html).toContain('/' + file);
    }
    expect(html).toContain('apple-mobile-web-app-title" content="KPDesk"');
  });

  it('contains correct source image dimensions', () => {
    expect(pngDimensions('favicon-96x96.png')).toEqual([96, 96]);
    expect(pngDimensions('apple-touch-icon.png')).toEqual([180, 180]);
    expect(pngDimensions('web-app-manifest-192x192.png')).toEqual([192, 192]);
    expect(pngDimensions('web-app-manifest-512x512.png')).toEqual([512, 512]);
    const favicon = readFileSync(inPublic('favicon.ico'));
    expect(favicon.readUInt16LE(0)).toBe(0);
    expect(favicon.readUInt16LE(2)).toBe(1);
  });

  it('has a valid site manifest with local reachable icon paths', () => {
    const manifest = JSON.parse(readFileSync(inPublic('site.webmanifest'), 'utf8')) as {
      name: string;
      short_name: string;
      icons: Array<{ src: string; sizes: string; type: string }>;
      theme_color: string;
    };
    expect(manifest.name).toBe('KPDesk');
    expect(manifest.short_name).toBe('KPDesk');
    expect(manifest.icons).toHaveLength(2);
    for (const icon of manifest.icons) {
      expect(icon.src.startsWith('/')).toBe(true);
      expect(icon.type).toBe('image/png');
      expect(statSync(inPublic(icon.src.slice(1))).size).toBeGreaterThan(1000);
    }
    expect(html).toContain('name="theme-color" content="' + manifest.theme_color + '"');
  });
});
