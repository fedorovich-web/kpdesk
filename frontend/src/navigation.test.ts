import { describe, expect, it } from 'vitest';
import { pageFromPath, pagePaths, pageTitle } from './navigation';

describe('workspace navigation', () => {
  it('has a unique deep-link for each screen', () => {
    expect(new Set(Object.values(pagePaths)).size).toBe(Object.keys(pagePaths).length);
    for (const [page, path] of Object.entries(pagePaths)) {
      expect(pageFromPath(path)).toBe(page);
      expect(pageFromPath(path + '/')).toBe(page);
    }
  });

  it('handles the legacy root without silently routing unrelated URLs', () => {
    expect(pageFromPath('/')).toBe('home');
    expect(pageFromPath('/index.html')).toBe('home');
    expect(pageFromPath('/missing')).toBe('notFound');
    expect(pageTitle('notFound')).toBe('Страница не найдена');
  });
});
