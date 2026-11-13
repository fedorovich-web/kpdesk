import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';

const frontend = resolve(fileURLToPath(new URL('..', import.meta.url)));
const artifacts = resolve(frontend, '..', 'artifacts', 'visual');
const base = process.env.KPDESK_PREVIEW_URL ?? 'http://127.0.0.1:5173';
const chrome = process.env.CHROME_BIN ??
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

if (!existsSync(chrome)) throw new Error('Chrome not installed; set CHROME_BIN');
const health = await fetch(base).catch(() => null);
if (!health?.ok) throw new Error('Start Vite dev server before npm run test:visual');
mkdirSync(artifacts, { recursive: true });

const server = createServer();
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const port = server.address().port;
await new Promise((done) => server.close(done));
const profile = mkdtempSync(join(tmpdir(), 'kpdesk-visual-'));
const browser = spawn(chrome, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--remote-debugging-address=127.0.0.1',
  '--remote-debugging-port=' + port, '--user-data-dir=' + profile, 'about:blank',
], { stdio: 'ignore' });

let ws;
try {
  let targets;
  for (let attempt = 0; attempt < 60; attempt++) {
    const response = await fetch('http://127.0.0.1:' + port + '/json')
      .catch(() => null);
    if (response?.ok) {
      targets = await response.json();
      if (targets.some((item) => item.type === 'page')) break;
    }
    await sleep(150);
  }
  const target = targets?.find((item) => item.type === 'page');
  if (!target) throw new Error('Chrome DevTools endpoint unavailable');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, reject) => {
    ws.addEventListener('open', done, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let nextId = 0;
  const waiting = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(String(event.data));
    if (!waiting.has(message.id)) return;
    const { done, reject } = waiting.get(message.id);
    waiting.delete(message.id);
    message.error ? reject(new Error(message.error.message)) : done(message.result);
  });
  function command(method, params = {}) {
    return new Promise((done, reject) => {
      const id = ++nextId;
      waiting.set(id, { done, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }
  await command('Page.enable');
  await command('Runtime.enable');

  const viewports = process.env.KPDESK_VISUAL_WIDTHS
    ? process.env.KPDESK_VISUAL_WIDTHS.split(',').map(Number)
    : [320, 390, 768, 1024, 1280, 1440];
  const pages = process.env.KPDESK_VISUAL_ROUTES
    ? process.env.KPDESK_VISUAL_ROUTES.split(',')
    : ['/', '/faq', '/features', '/how-it-works', '/integrations', '/pricing', '/example-not-found', '/app', '/demo', '/demo/prices', '/demo/prices/import',
      '/demo/requests/new', '/demo/quotes/review', '/demo/quotes/preview'];
  const demoHeadings = {
    '/demo': 'Рабочая область KPDesk', '/demo/prices': 'Прайс-лист',
    '/demo/prices/import': 'Импорт прайс-листа', '/demo/requests/new': 'Новая заявка',
    '/demo/quotes/review': 'Проверка КП', '/demo/quotes/preview': 'Нет черновика для предпросмотра',
  };
  let failures = 0;
  for (const route of pages) {
    for (const width of viewports) {
      const height = width <= 390 ? 844 : 1000;
      await command('Emulation.setDeviceMetricsOverride', {
        width, height, deviceScaleFactor: 1, mobile: true,
      });
      await command('Page.navigate', { url: base + route });
      // A loaded document/title is not proof the React tree has painted.
      // A formerly blank 390px screenshot passed every overflow check.
      let pageReady = false;
      for (let attempt = 0; attempt < 50; attempt++) {
        const status = await command('Runtime.evaluate', {
          expression: "(() => {const h=document.querySelector('h1');return document.readyState==='complete' && !!h && h.getBoundingClientRect().height>0 && document.body.innerText.trim().length>80})()",
          returnByValue: true,
        });
        if (status.result.value === true) { pageReady = true; break; }
        await sleep(100);
      }
      if (!pageReady) throw new Error('Page did not render visible content: ' + route);
      if (demoHeadings[route]) {
        const heading = await command('Runtime.evaluate', {
          expression: "document.querySelector('h1')?.textContent?.trim()",
          returnByValue: true,
        });
        if (heading.result.value !== demoHeadings[route]) {
          throw new Error('Wrong demo screen rendered for ' + route + ': ' + heading.result.value);
        }
      }
      // Give fonts and compositing a frame before pixel capture.
      await command('Runtime.evaluate', {
        expression: "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
        awaitPromise: true,
      });
      await sleep(100);
      const drawerProbe = process.env.KPDESK_VISUAL_DRAWER === '1' && width <= 768;
      if (drawerProbe) {
        let opened = false;
        let diagnostic;
        // Navigation finishes before React hydration and the client JS load.
        // Poll for the real visible control instead of assuming a fixed delay.
        for (let attempt = 0; attempt < 35; attempt++) {
          const probe = await command('Runtime.evaluate', {
            expression: "(() => {const trigger = document.querySelector('.mobile-menu-toggle');if(!trigger || getComputedStyle(trigger).display==='none')return {ready:false,path:location.pathname,documentReady:document.readyState,body:document.body?.innerText.slice(0,120)};trigger.click();return {ready:true}})()",
            returnByValue: true,
          });
          diagnostic = probe.result.value;
          if (diagnostic?.ready) { opened = true; break; }
          await sleep(120);
        }
        if (!opened) throw new Error('Visible mobile navigation trigger missing: ' + JSON.stringify(diagnostic));
        await sleep(400);
      }
      if (route === '/demo/quotes/preview' && process.env.KPDESK_VISUAL_DEMO === '1') {
        const activated = await command('Runtime.evaluate', {
          expression: "(() => {const button = [...document.querySelectorAll('button')].find(x => x.textContent?.includes('Посмотреть демопример из макета'));if(!button)return false;button.click();return true})()",
          returnByValue: true,
        });
        if (!activated.result.value) throw new Error('Preview demo action was not found');
        await sleep(200);
      }
      const evaluated = await command('Runtime.evaluate', {
        expression: `JSON.stringify((() => {
          const el = document.documentElement;
          const broken = Array.from(document.images)
            .filter(img => img.complete && img.naturalWidth === 0)
            .map(img => img.getAttribute('src'));
          const tiny = Array.from(document.querySelectorAll('body *'))
            .filter(node => node.children.length === 0 && node.textContent.trim()
              && node.getClientRects().length
              && Number.parseFloat(getComputedStyle(node).fontSize) < 14
              && !node.closest('.breadcrumbs, .copyright'))
            .map(node => node.tagName + ':' + node.textContent.trim().slice(0, 35));
          const clippedHeadings = [...document.querySelectorAll('h1,h2')]
            .filter(node => {
              if (node.scrollWidth > node.clientWidth + 1) return true;
              const box = node.getBoundingClientRect();
              for (let parent = node.parentElement; parent; parent = parent.parentElement) {
                const overflow = getComputedStyle(parent).overflowX;
                if (overflow === 'hidden' || overflow === 'clip') {
                  const parentBox = parent.getBoundingClientRect();
                  if (box.right > parentBox.right + 1 || box.left < parentBox.left - 1) return true;
                }
              }
              return false;
            }).map(node => node.textContent.trim().slice(0,55));
          return { viewport: innerWidth, content: el.scrollWidth, broken, tiny, clippedHeadings,
            title: document.title };
        })())`,
        returnByValue: true,
      });
      const result = JSON.parse(evaluated.result.value);
      if (drawerProbe) {
        const probe = await command('Runtime.evaluate', {
          expression: "JSON.stringify((()=>{const panel=document.querySelector('.mobile-drawer');const overlay=document.querySelector('.mobile-drawer-overlay');const trigger=document.querySelector('.mobile-menu-toggle');const rect=panel?.getBoundingClientRect();return {expanded:trigger?.getAttribute('aria-expanded'),visibility:getComputedStyle(panel).visibility,transform:getComputedStyle(panel).transform,transition:getComputedStyle(panel).transitionDuration,overlayOpacity:getComputedStyle(overlay).opacity,scrollLocked:document.body.style.overflow==='hidden',right:rect?.right,width:rect?.width}})())",
          returnByValue: true,
        });
        result.drawer = JSON.parse(probe.result.value);
      }
      const okay = result.content <= width + 1 && result.broken.length === 0 &&
        result.tiny.length === 0 && result.clippedHeadings.length === 0 &&
        (!drawerProbe || (result.drawer.expanded === 'true' && result.drawer.visibility === 'visible' &&
          result.drawer.overlayOpacity === '1' && result.drawer.scrollLocked &&
          Math.abs(result.drawer.right - width) < 2 &&
          result.drawer.width < width - 24));
      if (!okay) {
        failures++;
        const diagnostic = await command('Runtime.evaluate', {
          expression: "JSON.stringify([...document.querySelectorAll('html,body,body *')].map(node => {const r=node.getBoundingClientRect();return {tag:node.tagName,cls:typeof node.className === 'string' ? node.className.slice(0,60) : '',text:node.textContent?.trim().slice(0,25),left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),client:node.clientWidth,scroll:node.scrollWidth,minWidth:getComputedStyle(node).minWidth};}).filter(x=>(x.scroll>x.client+1)||(x.right>" + (width + 1) + " && x.width > 0)).sort((a,b)=>b.scroll-b.client-a.scroll+a.client).slice(0,20))",
          returnByValue: true,
        });
        console.log('OVERFLOW ELEMENTS', route, width, diagnostic.result.value);
      }
      console.log(JSON.stringify({ route, width, ok: okay, ...result }));
      if (route === '/demo/quotes/preview' && process.env.KPDESK_VISUAL_PDF === '1') {
        // The app banner is hidden when printing. Provenance must be in the sheet itself.
        const sheet = await command('Runtime.evaluate', {
          expression: "(() => {const sheet=document.querySelector('.print-sheet');return !!sheet && sheet.innerText.includes('ДЕМО — НЕ ЯВЛЯЕТСЯ КП') && sheet.innerText.includes('Демонстрационные данные')})()",
          returnByValue: true,
        });
        if (!sheet.result.value) throw new Error('Printable synthetic sheet missing; activate the explicit demo example before PDF verification');
        const printed = await command('Page.printToPDF', {
          printBackground: true,
          preferCSSPageSize: true,
          transferMode: 'ReturnAsBase64',
        });
        const pdf = Buffer.from(printed.data, 'base64');
        if (pdf.subarray(0, 5).toString('ascii') !== '%PDF-' || pdf.length < 2000) {
          throw new Error('Chrome returned an invalid/empty PDF');
        }
        const suffix = process.env.KPDESK_VISUAL_DEMO === '1' ? '-demo' : '';
        const filename = 'demo-quotes-preview' + suffix + '-' + width + '.pdf';
        writeFileSync(join(artifacts, filename), pdf);
        console.log('PRINT PDF', filename, pdf.length, 'bytes');
      }
      if (['/', '/faq', '/features', '/how-it-works', '/integrations', '/pricing', '/example-not-found', '/app', ...Object.keys(demoHeadings)].includes(route)) {
        const capture = await command('Page.captureScreenshot', {
          format: 'png', captureBeyondViewport: false,
        });
        const slug = route === '/' ? 'public' : route === '/app' ? 'account-access' : route.slice(1).replaceAll('/', '-');
        writeFileSync(join(artifacts, slug +
          (process.env.KPDESK_VISUAL_DEMO === '1' && route === '/demo/quotes/preview' ? '-demo' : '') +
          (drawerProbe ? '-menu' : '') + '-' + width + '.png'),
          Buffer.from(capture.data, 'base64'));
      }
      if (drawerProbe) {
        await command('Runtime.evaluate', {
          expression: "document.querySelector('.mobile-drawer-overlay').click()",
        });
        await sleep(350);
        const closed = await command('Runtime.evaluate', {
          expression: "(() => {const trigger=document.querySelector('.mobile-menu-toggle');const layer=document.querySelector('.mobile-drawer-layer');return trigger?.getAttribute('aria-expanded')==='false' && document.body.style.overflow!=='hidden' && getComputedStyle(layer).visibility==='hidden' && document.activeElement===trigger})()",
          returnByValue: true,
        });
        if (!closed.result.value) {
          failures++;
          console.error('Mobile drawer failed to close and restore focus/scroll', route, width);
        }
      }
    }
  }
  console.log('VISUAL VERIFICATION:', failures ? failures + ' FAILURES' : 'ALL CHECKS PASSED');
  if (failures) process.exitCode = 1;
} finally {
  ws?.close();
  browser.kill();
  await sleep(350);
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); }
  catch { console.error('Chrome temporary profile is locked:', profile); }
}
