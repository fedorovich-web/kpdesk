import React from 'react';
import { createRoot, hydrateRoot } from 'react-dom/client';
import { App } from './App';
import { PublicHome } from './components/PublicHome';
import { FaqPage } from './components/FaqPage';
import { FeaturesPage } from './components/FeaturesPage';
import { HowItWorksPage } from './components/HowItWorksPage';
import { IntegrationsPage } from './components/IntegrationsPage';
import { PricingPage } from './components/PricingPage';
import { PrivacyPage } from './components/PrivacyPage';
import { NotFoundPage } from './components/NotFoundPage';
import './styles.css';

// Private session/workspace code is never needed for public route hydration.
const AccountAccess = React.lazy(async () => ({
  default: (await import('./components/AccountAccess')).AccountAccess,
}));
const SharedProposalPage = React.lazy(async () => ({
  default: (await import('./components/SharedProposalPage')).SharedProposalPage,
}));

const normalizedPath = (path: string) => path.replace(/\/+$/, '') || '/';

function RootRoute() {
  const path = normalizedPath(window.location.pathname);
  if (path === '/' || path === '/index.html') return <PublicHome />;
  if (path === '/faq') return <FaqPage />;
  if (path === '/features') return <FeaturesPage />;
  if (path === '/how-it-works') return <HowItWorksPage />;
  if (path === '/integrations') return <IntegrationsPage />;
  if (path === '/pricing') return <PricingPage />;
  if (path === '/privacy') return <PrivacyPage />;
  if (path === '/demo' || path.startsWith('/demo/')) return <App />;
  if (path === '/proposal') return <React.Suspense fallback={<main role="status">Открываем КП…</main>}>
    <SharedProposalPage/>
  </React.Suspense>;
  if (path.startsWith('/auth/') || path === '/app' || path.startsWith('/app/')) {
    return <React.Suspense fallback={<main role="status">Загрузка личного кабинета…</main>}>
      <AccountAccess />
    </React.Suspense>;
  }
  return <NotFoundPage />;
}

const root = document.getElementById('root')!;
const app = <React.StrictMode><RootRoute /></React.StrictMode>;
if (root.getAttribute('data-ssr-route') === normalizedPath(window.location.pathname)) {
  hydrateRoot(root, app);
} else {
  // A private route or unknown SPA fallback must not hydrate public marketing HTML.
  root.replaceChildren();
  createRoot(root).render(app);
}
