# KPDesk frontend: design tokens and local typography

React 19 + TypeScript + Vite. Current frontend UI is an implementation in progress. Final visual matching to user-provided PNG layouts and use of original hero/icon archives require a separate visual comparison and import of those assets onto Home-PC. Avoid representing placeholder layouts as pixel-identical.

## Chosen typeface

**Inter Variable** (SIL Open Font License 1.1), locally served from `public/fonts/inter/`. Its single variable weight axis covers **100–900**, but use only **400 / 500 / 600 / 700 / 800** in the design system, without separately downloading five files.

| Semantic role | CSS token | Weight |
| --- | --- | ---: |
| Body, product tables, descriptions, inputs | `--weight-regular` | 400 |
| Labels, navigation | `--weight-medium` | 500 |
| CTA buttons, SKU titles | `--weight-semibold` | 600 |
| H1–H3, key numbers | `--weight-bold` | 700 |
| Main dashboard hero H1 | `--weight-extrabold` | 800 |

Semantic heading sizes in `src/styles/tokens.css`: `--text-h1: clamp(25px, 2.1vw, 34px)`, `--text-h2: 18px`, `--text-h3: 17px`, `--text-h4: 16px`. The dashboard hero uses `--text-hero: clamp(27px, 2.9vw, 41px)`. Other text sizes, line heights, weights, tracking, colors and gradients are defined centrally. The final sizes must remain consistent with the provided approved layouts when visually comparing.

## Shipping optimized font files

```
frontend/
  public/fonts/inter/
    inter-cyrillic-wght-normal.woff2   18,748 bytes
    inter-latin-wght-normal.woff2      48,256 bytes
    inter-ruble-wght-normal.woff2       1,196 bytes
    OFL.txt
  src/styles/
    fonts.css
    tokens.css
    tokens.test.ts
  src/styles.css
```

**Total WOFF2 payload: 68,200 bytes (~66.6 KiB)**. The Cyrillic Unicode range covers Russian, the Latin range covers English, and the third custom subset covers only U+20BD `₽`. This is significantly smaller than shipping the entire Latin Extended subset. All three use `font-display: swap`, `font-weight: 100 900` and local `/fonts/inter/...` URLs. The browser requests only the needed subsets. No Google Fonts CDN is requested.

Source package `@fontsource-variable/inter` is a **development dependency**, not a runtime import; license copied as `OFL.txt`. Ruble subset generated using FontTools (variable fvar/gvar tables retained):

```powershell
uvx --from "fonttools[woff]" pyftsubset "node_modules/@fontsource-variable/inter/files/inter-latin-ext-wght-normal.woff2" --unicodes=U+20BD --flavor=woff2 --layout-features='*' --output-file="public/fonts/inter/inter-ruble-wght-normal.woff2"
```

Do not copy font files into `src/`, the JS bundle, or external CDNs. Changes to `fonts.css` and the physical file should be reviewed together.

## Design system editing

Only change shared variables in `src/styles/tokens.css` when adjusting colors, spacing, typography, hover, gradients, radii, box shadows, breakpoints and interactive states. Component rules in `src/styles.css` refer to those tokens and do not embed their own HEX colors or font sizes. Layout-specific constants are named, and spacing sizes are centralized. Responsive overrides are declared with modern CSS media ranges, logical CSS properties and `prefers-reduced-motion` support.

Checks:

```powershell
cd C:\Work\kpdesk\frontend
npm test
npm run build
```

`tokens.test.ts` asserts no undefined/unused design tokens, no raw hex/font-size/gradients in component CSS, no Google Fonts dependency, correct local font files/permission document and support for the ruble character. If fonts or layout tokens change, tests must continue to pass.


## Typography size floor — mandatory, 2026-10-08

- **Minimum visible UI font size: 14 px**, including controls, navigation, table cells, placeholders, labels, card metadata and mobile UI.
- Only **breadcrumbs and footer copyright** are allowed at **12 px**; do not extend this exception to other secondary text.
- The prior 10–13 px tokens were removed. Use `--text-base: 14px` and `--text-breadcrumb: 12px`; H1–H4 and larger sizes remain under `src/styles/tokens.css`.
- `src/styles/tokens.test.ts` includes a regression test for this font floor. To change design, edit tokens first, then run `npm test` and `npm run build`.
- The user's definitive design reference is supplied separately in `KPDesk_DESIGN_HANDOFF_ALL.zip` with 21 PNG layouts, 13 hero AVIF, 36 icon AVIF, 4 supplemental images and logo. As of this handoff the raw reference archive is **not yet extracted onto Home-PC**; do not claim pixel-level visual completion.

## Design handoff (2026-10-08)

Source PNG: `../design/reference/` (read-only). Asset URL map: `src/design-assets.ts`; files: `public/assets/{hero,icons,supporting}`. Original RGB logo `public/assets/logo.avif` is preserved, while the UI uses `public/assets/logo-transparent.avif` (derived alpha matte repair).

Implemented URLs: `/`, `/faq` and workspace screens `/app`, `/app/prices`, `/app/prices/import`, `/app/requests/new`, `/app/quotes/review`. Other workspace URLs show explicit unavailable states pending secure backend sessions.

With Vite running on localhost, `npm run test:visual` performs real Chrome responsive checks and screenshots for 7 routes at 320/390/768/1024/1280/1440px. The screenshots are written to `../artifacts/visual/`. `CHROME_BIN` and `KPDESK_PREVIEW_URL` may override executable/server address. This is not a full pixel-diff or WCAG audit.

See `../docs/design-reference-map.md`, `../docs/seo-content.md` and `../docs/verification.md`. No Git operations are part of this workflow.

## Favicon / browser icons

Use the approved favicon pack saved at `../design/favicon.zip`. Installation and source SHA-256 verification are reproducible via `python ../design/install_favicon.py` (Python 3.12+). Seven original assets are copied directly to `public/`, with paths used by `index.html` and `site.webmanifest`. The manifest icons and Apple Touch Icon are **not** generated from the wordmark; they come from the uploaded favicon package. `src/favicon.test.ts` verifies package integration. Vite includes all files in `dist/`.

## Public page implementation — 2026-10-08

Independent marketing screens: `/features`, `/how-it-works`, `/integrations`, `/pricing` (approved PNG #02–05), shared `components/MarketingShared.tsx`, marketing content in `src/content/marketing.ts`, tokens in `src/styles/tokens.css`, screen CSS in `src/styles/marketing.css`. 404 screen: `components/NotFoundPage.tsx`, `src/styles/not-found.css`, hero `13_hero_404.avif`.

Real working URLs are handled by `src/main.tsx`; unknown public paths show the public 404 instead of a dashboard. Public pricing mock cards and their old `src/demo/pricing.ts` source were removed. The pricing page explains cost factors without an invented checkout. Production builds pre-render public routes and verify real HTTP HTML responses; CRM/1C adapter contracts are not live integrations.

`npm run test:visual` checks routes at 320/390/768/1024/1280/1440 px. For targeted debugging set `KPDESK_VISUAL_ROUTES` (comma-separated paths) and `KPDESK_VISUAL_WIDTHS` (comma-separated pixel widths). Screenshot files are in `../artifacts/visual`; this check is not pixel-perfect or a W3C audit.

## Workspace #11–14 implementation (2026-10-08)

`src/components/CatalogPage.tsx` handles the real catalog list with local pagination, sorting and API error state. `src/components/ImportCatalogPage.tsx` accepts only CSV/XLSX up to 10 MiB, reuses the backend atomic import API, provides real drag/drop and a **header-only** `public/templates/kpdesk-catalog-v2-columns.csv` file. Import requires the backend's exact columns; column mapping from the PNG mockup is not a shipped feature. `src/components/ReviewPage.tsx` shows actual draft line statuses, no fabricated supplier data, and hides total when any item remains ambiguous. The route state and shared request state remain in `src/App.tsx`.

`src/App.test.tsx` simulates the API response and verifies that import success navigates back to `/app/prices` with its success notice. Component tests validate all three screens and client validation. `npm run test:visual` also captures workspace route screenshots. The local backend currently returns an HTTP 500 in browser catalog requests; do not assert that live import works end-to-end. Runtime DB must not be started with owner credentials.

## Workspace request screen #13 (2026-10-08)

`src/components/RequestPage.tsx` implements the actual source-text → Groq extraction → editable product lines → auto-draft UX. `src/components/WorkflowSteps.tsx` is a three-step workflow matching the actual backend API. `src/styles/request.css` holds the responsive approved reference layout; styling remains tokenized in `src/styles/tokens.css`. The request component remains API-agnostic: App owns source text, model, extracted lines, catalog and mutations.

Any change to the source text or selected model invalidates old extracted lines and draft. The resulting AI response and computed prices **are never substituted by design fixture data**. Unsupported mockup fields (shipping address, deadline, supplier) are not persisted or exposed as working controls. Tests live in `src/components/RequestPage.test.tsx` and `src/App.test.tsx`. This is not a confirmed 1:1 pixel-diff of screenshot #13.

## Client-side preview #15 (2026-10-08)

`/app/quotes/preview` renders `src/components/PreviewPage.tsx`: **only the current tab's real, unapproved AutoDraft and ExtractedRequest**. It supports three actual CSS presentation themes, optional logo/reasons, zoom, and system printing (Save as PDF in Chrome), with dedicated `src/styles/preview.css` print styles. It does not approve, persist or send documents; client/contractor details, VAT and shipment terms are deliberately not invented.

To visually inspect #15 without an API session, click the explicit `Посмотреть демопример из макета` action when the page has no real draft. Approved mockup sample is isolated in `src/demo/preview.ts`, never sent to APIs, and marked DEMO on screen and printed paper. Demo sample total excludes unsupported VAT.

`npm run test:visual` supports `KPDESK_VISUAL_ROUTES=/app/quotes/preview` plus `KPDESK_VISUAL_DEMO=1` for screenshotting filled fixture and `KPDESK_VISUAL_PDF=1` to smoke-test Chrome print-to-PDF. Test PDF output is saved under `../artifacts/visual`; use `KPDESK_VISUAL_WIDTHS=1440` to limit the PDF test to desktop. This is no substitute for protected backend draft PDF, server role/authentication or pixel-perfect comparison.
