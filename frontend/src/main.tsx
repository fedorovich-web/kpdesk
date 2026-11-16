import React from 'react';
import { createRoot, hydrateRoot } from 'react-dom/client';
import './public-styles.css';

// Private session/workspace code is never needed for public route hydration.
const AccountAccess = React.lazy(async () => ({
  default: (await import('./components/AccountAccess')).AccountAccess,
}));
const SharedProposalPage = React.lazy(async () => ({
  default: (await import('./components/SharedProposalPage')).SharedProposalPage,
}));

const normalizedPath = (path: string) => path.replace(/\/+$/, '') || '/';

type DemoComponent = typeof import('./App').App;

type PublicPageComponent = React.ComponentType;

/**
 * Only load the public page the visitor actually requested.
 * The server already sends prerendered, crawlable markup while the route chunk
 * downloads, so this does not block the initial HTML/LCP paint.
 */
async function loadPublicPage(path: string): Promise<PublicPageComponent | null> {
  if (path === '/' || path === '/index.html') return (await import('./components/PublicHome')).PublicHome;
  if (path === '/faq') return (await import('./components/FaqPage')).FaqPage;
  if (path === '/features') return (await import('./components/FeaturesPage')).FeaturesPage;
  if (path === '/how-it-works') return (await import('./components/HowItWorksPage')).HowItWorksPage;
  if (path === '/integrations') return (await import('./components/IntegrationsPage')).IntegrationsPage;
  if (path === '/pricing') return (await import('./components/PricingPage')).PricingPage;
  if (path === '/privacy') return (await import('./components/PrivacyPage')).PrivacyPage;
  if (path === '/demo' || path.startsWith('/demo/') || path === '/proposal'
    || path.startsWith('/auth/') || path === '/app' || path.startsWith('/app/')) {
    return null;
  }
  return (await import('./components/NotFoundPage')).NotFoundPage;
}

function RootRoute({ DemoApp, PublicPage }: {
  DemoApp: DemoComponent | null;
  PublicPage: PublicPageComponent | null;
}) {
  const path = normalizedPath(window.location.pathname);
  if (PublicPage) return <PublicPage />;
  if (path === '/demo' || path.startsWith('/demo/')) return DemoApp ? <DemoApp /> : null;
  if (path === '/proposal') return <React.Suspense fallback={<main role="status">Открываем КП…</main>}>
    <SharedProposalPage/>
  </React.Suspense>;
  if (path.startsWith('/auth/') || path === '/app' || path.startsWith('/app/')) {
    return <React.Suspense fallback={<main role="status">Загрузка личного кабинета…</main>}>
      <AccountAccess />
    </React.Suspense>;
  }
  return null;
}
async function mount() {
  const path = normalizedPath(window.location.pathname);
  // Only the interactive demo needs its workspace bundle. Pre-rendered demo
  // HTML remains visible while its module downloads, then hydrates normally.
  const [PublicPage, DemoApp] = await Promise.all([
    loadPublicPage(path),
    path === '/demo' || path.startsWith('/demo/')
      ? import('./App').then(module => module.App)
      : Promise.resolve(null),
  ]);
  const root = document.getElementById('root')!;
  const app = <React.StrictMode><RootRoute DemoApp={DemoApp} PublicPage={PublicPage} /></React.StrictMode>;
  if (root.getAttribute('data-ssr-route') === path) {
    hydrateRoot(root, app);
  } else {
    // A private route or unknown SPA fallback must not hydrate public marketing HTML.
    root.replaceChildren();
    createRoot(root).render(app);
  }
}

void mount();
