/** URL-backed navigation. These paths describe implemented UI, not backend endpoints. */
export const pagePaths = {
  home: '/demo',
  prices: '/demo/prices',
  import: '/demo/prices/import',
  request: '/demo/requests/new',
  review: '/demo/quotes/review',
  preview: '/demo/quotes/preview',
} as const;

export type Page = keyof typeof pagePaths | 'notFound';

export function pageFromPath(pathname: string): Page {
  if (pathname === '/' || pathname === '/index.html') return 'home';
  const pathnameWithoutSlash = pathname.replace(/\/$/, '');
  for (const [page, path] of Object.entries(pagePaths)) {
    if (pathnameWithoutSlash === path) return page as keyof typeof pagePaths;
  }
  return 'notFound';
}

export function pageTitle(page: Page): string {
  const titles: Record<Page, string> = {
    home: 'Главная',
    prices: 'Прайс-лист',
    import: 'Импорт прайс-листа',
    request: 'Новая заявка',
    review: 'Проверка КП',
    preview: 'Предпросмотр КП',
    notFound: 'Страница не найдена',
  };
  return titles[page];
}
