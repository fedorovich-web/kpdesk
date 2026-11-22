import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { createServer } from 'vite';
import React from 'react';
import { renderToString } from 'react-dom/server';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');
const domain = 'https://kpdesk.ru';
const routes = [
  { path: '/', module: '/src/components/PublicHome.tsx', exportName: 'PublicHome', seo: 'public' },
  { path: '/features', module: '/src/components/FeaturesPage.tsx', exportName: 'FeaturesPage', seo: 'features' },
  { path: '/how-it-works', module: '/src/components/HowItWorksPage.tsx', exportName: 'HowItWorksPage', seo: 'how' },
  { path: '/integrations', module: '/src/components/IntegrationsPage.tsx', exportName: 'IntegrationsPage', seo: 'integrations' },
  { path: '/pricing', module: '/src/components/PricingPage.tsx', exportName: 'PricingPage', seo: 'pricing' },
  { path: '/demo', module: '/src/App.tsx', exportName: 'App', seo: 'demo' },
  { path: '/faq', module: '/src/components/FaqPage.tsx', exportName: 'FaqPage', seo: 'faq' },
  { path: '/privacy', module: '/src/components/PrivacyPage.tsx', exportName: 'PrivacyPage', seo: 'privacy' },
];
const escapeHtml = value => value.replaceAll('&', '&amp;').replaceAll('"', '&quot;')
  .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const changeTag = (document, regex, value, label) => {
  if (!regex.test(document)) throw new Error('Missing required HTML tag: ' + label);
  return document.replace(regex, value);
};

const vite = await createServer({
  root,
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  logLevel: 'error',
});
try {
  const { metadata } = await vite.ssrLoadModule('/src/seo.ts');
  const template = await readFile(join(dist, 'index.html'), 'utf8');
  const manifest = JSON.parse(await readFile(join(dist, '.vite/manifest.json'), 'utf8'));
  const appBundle = manifest['index.html']?.file;
  const liteBundle = manifest['src/public-lite.ts']?.file;
  if (typeof appBundle !== 'string' || typeof liteBundle !== 'string'
    || !/^assets\/[A-Za-z0-9_-]+\.js$/.test(appBundle)
    || !/^assets\/[A-Za-z0-9_-]+\.js$/.test(liteBundle)
    || !template.includes('src="/' + appBundle + '"')) {
    throw new Error('Vite did not emit both required entrypoints.');
  }
  const lightweightHomeTemplate = template.replace('src="/' + appBundle + '"',
    'src="/' + liteBundle + '"');
  for (const route of routes) {
    const module = await vite.ssrLoadModule(route.module);
    const Component = module[route.exportName];
    if (typeof Component !== 'function') throw new Error('Missing component ' + route.exportName);
    // PublicChrome only reads pathname for navigation state; never use private cookies.
    globalThis.window = { location: { pathname: route.path } };
    const body = renderToString(React.createElement(React.StrictMode, null, React.createElement(Component)));
    if (!body.includes('<main') || !body.includes('<h1')) {
      throw new Error('Incomplete public HTML for ' + route.path);
    }
    const item = metadata[route.seo];
    const canonical = domain + route.path;
    const robots = route.seo === 'privacy' ? 'noindex,follow' : 'index,follow';
    // The homepage has only links, native FAQ details and a mobile menu.
    // Use its dedicated lightweight enhancement instead of loading React DOM.
    let html = route.path === '/' ? lightweightHomeTemplate : template;
    html = changeTag(html, /<title>[^<]*<\/title>/, '<title>' + escapeHtml(item.title) + '</title>', 'title');
    html = changeTag(html, /<meta name="description"[^>]*\/>/,
      '<meta name="description" content="' + escapeHtml(item.description) + '"/>', 'description');
    html = changeTag(html, /<meta name="robots"[^>]*\/>/,
      '<meta name="robots" content="' + robots + '"/>', 'robots');
    html = changeTag(html, /<meta property="og:title"[^>]*\/>/,
      '<meta property="og:title" content="' + escapeHtml(item.title) + '"/>', 'og:title');
    html = changeTag(html, /<meta property="og:description"[^>]*\/>/,
      '<meta property="og:description" content="' + escapeHtml(item.description) + '"/>', 'og:description');
    html = changeTag(html, /<meta property="og:url"[^>]*\/>/,
      '<meta property="og:url" content="' + canonical + '"/>', 'og:url');
    for (const name of ['title', 'description']) {
      html = changeTag(html, new RegExp('<meta name="twitter:' + name + '"[^>]*/>'),
        '<meta name="twitter:' + name + '" content="' + escapeHtml(item[name]) + '"/>', 'twitter:' + name);
    }
    html = changeTag(html, /<link rel="canonical"[^>]*\/>/,
      '<link rel="canonical" href="' + canonical + '"/>', 'canonical');
    html = changeTag(html, /<div id="root"><\/div>/,
      '<div id="root" data-ssr-route="' + route.path + '">' + body + '</div>', 'root');
    if (route.seo !== 'privacy') {
      const structuredData = {
        '@context': 'https://schema.org',
        '@graph': [
          { '@type': 'WebSite', '@id': domain + '/#website', name: 'KPDesk', url: domain + '/', inLanguage: 'ru' },
          { '@type': 'SoftwareApplication', '@id': domain + '/#software', name: 'KPDesk', url: domain + '/',
            applicationCategory: 'BusinessApplication', operatingSystem: 'Web', inLanguage: 'ru' },
          { '@type': 'WebPage', '@id': canonical + '#webpage', url: canonical, name: item.title,
            description: item.description, inLanguage: 'ru', isPartOf: { '@id': domain + '/#website' },
            about: { '@id': domain + '/#software' } },
        ],
      };
      html = html.replace('</head>', '<script type="application/ld+json">'
        + JSON.stringify(structuredData).replaceAll('<', '\\u003c') + '</script></head>');
    }
    // The prerendered route contains its own readable HTML and route-specific meta tags.
    if (!html.includes(item.description) || !html.includes('data-ssr-route="' + route.path + '"')) {
      throw new Error('Missing generated SEO markup for ' + route.path);
    }
    const output = route.path === '/' ? join(dist, 'index.html')
      : join(dist, route.path.slice(1), 'index.html');
    await mkdir(resolve(output, '..'), { recursive: true });
    await writeFile(output, html);
    console.log('prerender: ' + route.path);
  }
  // Static noindex page for unknown public routes; do not serve homepage SSR.
  const { NotFoundPage } = await vite.ssrLoadModule('/src/components/NotFoundPage.tsx');
  globalThis.window = { location: { pathname: '/unknown' } };
  const notFoundBody = renderToString(React.createElement(NotFoundPage));
  let notFoundHtml = template;
  notFoundHtml = changeTag(notFoundHtml, /<title>[^<]*<\/title>/,
    '<title>Страница не найдена — KPDesk</title>', '404 title');
  notFoundHtml = changeTag(notFoundHtml, /<meta name="robots"[^>]*\/>/,
    '<meta name="robots" content="noindex,nofollow"/>', '404 robots');
  notFoundHtml = changeTag(notFoundHtml, /<meta name="description"[^>]*\/>/,
    '<meta name="description" content="Страница не найдена — KPDesk."/>', '404 description');
  notFoundHtml = changeTag(notFoundHtml, /<link rel="canonical"[^>]*\/>/,
    '', '404 canonical');
  notFoundHtml = changeTag(notFoundHtml, /<div id="root"><\/div>/,
    '<div id="root">' + notFoundBody + '</div>', '404 root');
  await writeFile(join(dist, '404.html'), notFoundHtml);

  const privateRoutes = [
    '/app', '/app/prices', '/app/prices/import', '/app/requests/new',
    '/app/quotes/review', '/app/quotes/preview', '/app/quotes/history',
    '/app/clients', '/app/settings', '/auth/login', '/auth/register',
    '/auth/resend-confirmation', '/auth/verify-email', '/auth/forgot-password',
    '/auth/reset-password', '/auth/join', '/auth/organizations',
  ];
  let shell = template;
  shell = changeTag(shell, /<title>[^<]*<\/title>/,
    '<title>Кабинет — KPDesk</title>', 'shell title');
  shell = changeTag(shell, /<meta name="robots"[^>]*\/>/,
    '<meta name="robots" content="noindex,nofollow"/>', 'shell robots');
  shell = changeTag(shell, /<meta name="description"[^>]*\/>/,
    '<meta name="description" content="Рабочий кабинет KPDesk."/>', 'shell description');
  shell = changeTag(shell, /<link rel="canonical"[^>]*\/>/, '', 'private shell canonical');
  shell = changeTag(shell, /<meta property="og:url"[^>]*\/>/, '', 'private shell social URL');
  for (const path of privateRoutes) {
    const file = join(dist, path.slice(1), 'index.html');
    await mkdir(resolve(file, '..'), { recursive: true });
    await writeFile(file, shell);
  }
  const adminShell = changeTag(changeTag(shell, /<title>[^<]*<\/title>/,
    '<title>Кабинет владельца — KPDesk</title>', 'admin title'), /<meta name="description"[^>]*\/>/,
    '<meta name="description" content="Кабинет владельца KPDesk."/>', 'admin description');
  await mkdir(join(dist, 'admin'), { recursive: true });
  await writeFile(join(dist, 'admin/index.html'), adminShell);
  let proposalShell = changeTag(shell, /<title>[^<]*<\/title>/,
    '<title>Коммерческое предложение — KPDesk</title>', 'proposal title');
  proposalShell = changeTag(proposalShell, /<meta name="description"[^>]*\/>/,
    '<meta name="description" content="Утверждённое коммерческое предложение."/>', 'proposal description');
  proposalShell = proposalShell.replace('</head>', '<meta name="referrer" content="no-referrer"/></head>');
  await mkdir(join(dist, 'proposal'), { recursive: true });
  await writeFile(join(dist, 'proposal/index.html'), proposalShell);
  const siteRoutes = routes.filter(route => route.seo !== 'privacy')
    .map(route => '  <url><loc>' + domain + route.path + '</loc></url>').join('\n');
  await writeFile(join(dist, 'sitemap.xml'),
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + siteRoutes + '\n</urlset>\n');
  await writeFile(join(dist, 'robots.txt'),
    'User-agent: *\nDisallow: /app\nDisallow: /auth\nDisallow: /proposal\nDisallow: /admin\n'
    + 'Sitemap: ' + domain + '/sitemap.xml\n');
} finally {
  delete globalThis.window;
  await vite.close();
}
