import assert from 'node:assert/strict';

const origin = process.env.KPDESK_SMOKE_ORIGIN ?? 'http://127.0.0.1:4179';
const pageCases = [
  ['/', 'ИИ-генератор коммерческих предложений с подбором товаров', true],
  ['/features', 'ИИ-генерация текста КП и подбор товаров', true],
  ['/features/', 'ИИ-генерация текста КП и подбор товаров', true],
  ['/how-it-works', 'Как создать КП из заявки и прайса', true],
  ['/integrations', 'Ваш прайс — основа подбора и расчёта КП', true],
  ['/pricing', 'Тарифы KPDesk', true],
  ['/demo', 'Подготовьте предложение клиенту', true],
  ['/faq', 'Вопросы и ответы о KPDesk', true],
  ['/faq/', 'Вопросы и ответы о KPDesk', true],
  ['/privacy', 'Политика конфиденциальности', false],
];
const titleOf = html => /<title>([^<]+)<\/title>/.exec(html)?.[1];
for (const [path, heading, indexable] of pageCases) {
  const response = await fetch(new URL(path, origin), {
    signal: AbortSignal.timeout(5000), redirect: 'follow',
  });
  assert.equal(response.status, 200, path + ': status');
  const html = await response.text();
  const normalized = path.replace(/\/+$/, '') || '/';
  const servedH1 = (/<h1\b[^>]*>([\s\S]*?)<\/h1>/.exec(html)?.[1] ?? '').replace(/<[^>]*>/g, '').trim();
  assert.ok(servedH1.includes(heading), path + ': missing page copy; served H1=' + servedH1 + '; title=' + titleOf(html));
  assert.ok(html.includes('data-ssr-route="' + normalized + '"'), path + ': not prerendered');
  assert.equal((html.match(/<h1(?:\s|>)/g) ?? []).length, 1, path + ': h1');
  assert.match(html, new RegExp('<meta name="robots" content="'
    + (indexable ? 'index,follow' : 'noindex,follow') + '"'), path + ': robots');
  assert.ok(html.includes('rel="canonical" href="https://kpdesk.ru' + normalized + '"'),
    path + ': wrong canonical');
  assert.ok(titleOf(html)?.includes('KPDesk'), path + ': title');
  console.log('HTTP public PASS', path, titleOf(html));
}
for (const path of ['/app', '/auth/login', '/app/quotes/review', '/app/unlisted-route']) {
  const response = await fetch(new URL(path, origin), { signal: AbortSignal.timeout(5000) });
  assert.equal(response.status, 200, path + ': status');
  const html = await response.text();
  assert.ok(html.includes('name="robots" content="noindex,nofollow"'), path + ': noindex');
  assert.ok(!html.includes('data-ssr-route='), path + ': public prerender leaked');
  console.log('HTTP private PASS', path);
}

const missing = await fetch(new URL('/a-page-that-does-not-exist', origin),
  { signal: AbortSignal.timeout(5000) });
assert.equal(missing.status, 404, 'unknown public route must not respond 200');
const missingHtml = await missing.text();
assert.ok(missingHtml.includes('Страница не найдена'), '404 must contain rendered explanation');
assert.ok(missingHtml.includes('noindex,nofollow'), '404 must not be indexed');
assert.ok(!missingHtml.includes('rel="canonical"'), '404 must not canonicalize to home');
console.log('HTTP unknown-route PASS /a-page-that-does-not-exist');
