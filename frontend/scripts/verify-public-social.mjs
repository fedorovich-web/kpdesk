import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { JSDOM } from 'jsdom';

const dist = resolve(import.meta.dirname, '../dist');
for (const route of ['/', '/features', '/how-it-works', '/integrations', '/pricing', '/faq', '/demo']) {
  const html = await readFile(join(dist, route === '/' ? 'index.html' : route.slice(1) + '/index.html'), 'utf8');
  const document = new JSDOM(html).window.document;
  const meta = name => document.querySelector(`meta[property="${name}"],meta[name="${name}"]`)?.content;
  assert.equal(meta('og:url'), 'https://kpdesk.ru' + route, route + ' social URL');
  assert.equal(meta('twitter:card'), 'summary', route + ' Twitter card');
  assert.equal(meta('twitter:title'), document.title, route + ' Twitter title');
  assert.equal(meta('twitter:description'), meta('description'), route + ' Twitter description');
  assert.equal(meta('og:image'), 'https://kpdesk.ru/web-app-manifest-512x512.png');
  assert.equal(meta('twitter:image'), meta('og:image'));
  assert.ok(meta('og:image:alt') && meta('twitter:image:alt'));
  assert.ok((await stat(join(dist, 'web-app-manifest-512x512.png'))).size > 0);
  const schemas = [...document.querySelectorAll('script[type="application/ld+json"]')].map(node => JSON.parse(node.textContent));
  assert.equal(schemas.length, 1, route + ' structured data block');
  const graph = schemas[0]['@graph'];
  assert.equal(schemas[0]['@context'], 'https://schema.org');
  assert.ok(graph.some(node => node['@type'] === 'WebPage' && node.url === 'https://kpdesk.ru' + route));
  assert.ok(graph.some(node => node['@type'] === 'SoftwareApplication' && node.name === 'KPDesk'));
  assert.ok(graph.every(node => !('aggregateRating' in node) && !('review' in node) && !('offers' in node)));
}
for (const route of ['app', 'auth/login', 'proposal']) {
  const html = await readFile(join(dist, route, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /property="og:url"/);
  assert.doesNotMatch(html, /application\/ld\+json/);
}
console.log('Public social metadata and structured data: PASS');
