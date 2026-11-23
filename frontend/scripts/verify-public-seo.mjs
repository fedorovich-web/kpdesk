import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { launchPlans, rubles } from '../src/content/pricing.ts';

const dist = resolve(import.meta.dirname, '..', 'dist');
const publicRoutes = [
  '/', '/features', '/how-it-works', '/integrations', '/pricing', '/faq', '/demo',
];
const titles = new Set();
const descriptions = new Set();
for (const route of publicRoutes) {
  const file = route === '/' ? join(dist, 'index.html')
    : join(dist, route.slice(1), 'index.html');
  const html = await readFile(file, 'utf8');
  assert.match(html, /<html lang="ru">/);
  assert.match(html, /<main[ >]/);
  assert.equal((html.match(/<h1[ >]/g) ?? []).length, 1, route + ' must contain a single H1');
  assert.match(html, /<meta name="robots" content="index,follow"\/>/);
  assert.doesNotMatch(html, /запланированные функции|в разработке|в подготовке|следующий этап/iu,
    route + ' contains internal roadmap language');
  assert.ok(html.includes('href="https://kpdesk.ru' + route + '"'), route + ' canonical');
  assert.ok(html.includes('data-ssr-route="' + route + '"'), route + ' prerendered');
  const title = /<title>([^<]+)<\/title>/.exec(html)?.[1];
  const description = /<meta name="description" content="([^"]+)"\/>/.exec(html)?.[1];
  assert.ok(title && !titles.has(title), route + ' unique title');
  assert.ok(description && !descriptions.has(description), route + ' unique description');
  titles.add(title);
  descriptions.add(description);
  assert.ok(html.includes('<meta property="og:description" content="'), route + ' og description');
}
const faq = await readFile(join(dist, 'faq', 'index.html'), 'utf8');
assert.match(faq, /Как создать коммерческое предложение по заявке/);
assert.match(faq, /Тарифы и оплата/);
assert.match(faq, /Что делать, если сервис не подобрал товар/);
assert.match(faq, /Оно не вызывает внешний ИИ/);
assert.match(faq, /Если в карточке КП доступен раздел «Ссылка для клиента»/);
const home = await readFile(join(dist, 'index.html'), 'utf8');
const pricing = await readFile(join(dist, 'pricing/index.html'), 'utf8');
for (const [html, name] of [[home, 'kpdesk-home-faq'], [pricing, 'kpdesk-pricing-faq'], [faq, 'kpdesk-help-faq']]) {
  const questions = html.match(/<details\b[^>]*>/g) ?? [];
  assert.ok(questions.length > 1 && questions.every(tag => tag.includes('name="' + name + '"')),
    name + ' must be exclusive in initial HTML');
}
const manifest = JSON.parse(await readFile(join(dist, '.vite/manifest.json'), 'utf8'));
const homeEntry = manifest['src/public-lite.ts']?.file;
const reactEntry = manifest['index.html']?.file;
assert.ok(typeof homeEntry === 'string' && typeof reactEntry === 'string',
  'Vite must emit both lightweight public and React application entrypoints');
assert.ok(home.includes('src="/' + homeEntry + '"'), 'homepage must use lightweight entrypoint');
assert.ok(!home.includes('src="/' + reactEntry + '"'), 'homepage must not load React');
assert.ok(pricing.includes('src="/' + reactEntry + '"'),
  'interactive pricing FAQ must keep React hydration');
const demo = await readFile(join(dist, 'demo/index.html'), 'utf8');
for (const file of manifest['src/App.tsx'].css) {
  assert.ok(demo.includes('href="/' + file + '"'), 'demo SSR needs workspace styles before hydration');
  assert.ok(!home.includes('href="/' + file + '"'), 'homepage must not load workspace styles');
}
assert.match(home, /<h1[^>]*><span>Создавайте<\/span> <span>коммерческие<\/span> <span>предложения с ИИ<\/span><\/h1>/);
assert.match(home, /Генерация текста КП/);
const features = await readFile(join(dist, 'features', 'index.html'), 'utf8');
assert.match(features, /ИИ пишет текст КП/);
assert.match(features, /KPDesk предложит позиции из каталога по требованиям заявки/);
assert.doesNotMatch(home, /от 5 990 ₽|за 5 минут вместо/i);
for (const route of ['/', '/pricing']) {
  const html = await readFile(route === '/' ? join(dist, 'index.html') : join(dist, 'pricing/index.html'), 'utf8');
  const prices = [...html.matchAll(/class="public-plan-price">([^<]+)</g)].map(match => match[1]);
  assert.deepEqual(prices, launchPlans.map(plan => rubles(plan.priceRub)), route + ' approved launch prices');
  assert.doesNotMatch(html, /Планируемые условия запуска|Приём оплаты и включение тарифов готовятся/);
  assert.doesNotMatch(html, /дешевле всех/iu);
  assert.match(html, /за 30 дней/);
  assert.doesNotMatch(html, /Оплата пока не подключена|планируемая цена/);
}

for (const route of ['/app', '/app/quotes/review', '/auth/login', '/auth/register', '/admin']) {
  const html = await readFile(join(dist, route.slice(1), 'index.html'), 'utf8');
  assert.match(html, /<meta name="robots" content="noindex,nofollow"\/>/);
  assert.doesNotMatch(html, /data-ssr-route=/);
  assert.doesNotMatch(html, /<h1[ >]/);
  assert.doesNotMatch(html, /<link\s+rel="canonical"/);
}
const proposal = await readFile(join(dist, 'proposal/index.html'), 'utf8');
assert.doesNotMatch(proposal, /<link\s+rel="canonical"/);
const errorPage = await readFile(join(dist, '404.html'), 'utf8');
assert.ok(errorPage.includes('Страница не найдена'));
assert.match(errorPage, /<meta name="robots" content="noindex,nofollow"\/>/);
assert.ok(!errorPage.includes('rel="canonical"'));
const privacy = await readFile(join(dist, 'privacy', 'index.html'), 'utf8');
assert.ok(privacy.includes('Федорович Р. О.'));
assert.ok(privacy.includes('mailto:support@kpdesk.ru'));
assert.ok(!privacy.includes('Роман Олегович'));
const sitemap = await readFile(join(dist, 'sitemap.xml'), 'utf8');
assert.ok(sitemap.includes('https://kpdesk.ru/features'));
assert.ok(!sitemap.includes('/privacy'));
assert.ok(!sitemap.includes('/auth'));
const robots = await readFile(join(dist, 'robots.txt'), 'utf8');
assert.ok(robots.includes('Disallow: /app'));
assert.ok(robots.includes('Disallow: /admin'));
assert.ok(robots.includes('Disallow: /auth'));
console.log('public prerender SEO verification: PASS (' + publicRoutes.length
  + ' indexable public pages, isolated private shells)');
