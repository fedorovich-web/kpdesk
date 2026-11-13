import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { verifySupplierBatchBrowser } from './tenant-batch-browser-steps.mjs';
import { verifySupplierLargeBrowser } from './tenant-large-review-browser-steps.mjs';
import { verifyPortfolioBrowser } from './tenant-portfolio-browser-steps.mjs';
import { verifyProposalBrowser } from './tenant-proposal-browser-steps.mjs';
import { verifyWorkspaceLayout } from './tenant-workspace-browser-steps.mjs';
import { verifyCompanyBrowser } from './tenant-company-browser-steps.mjs';
import { verifyVariantsBrowser } from './tenant-variants-browser-steps.mjs';

const url = process.env.KP_DESK_E2E_URL;
const email = process.env.KP_DESK_E2E_EMAIL;
const password = process.env.KP_DESK_E2E_PASSWORD;
const chrome = process.env.CHROME_BIN ??
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
if (!url?.startsWith('https://127.0.0.1:') || !email || !password) {
  throw new Error('Isolated HTTPS browser test configuration missing.');
}
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const exitedWithin = (child, milliseconds) => new Promise(resolve => {
  if (child.exitCode !== null || child.signalCode !== null) {
    resolve(true);
    return;
  }
  const onExit = () => {
    clearTimeout(timer);
    resolve(true);
  };
  const timer = setTimeout(() => {
    child.removeListener('exit', onExit);
    resolve(false);
  }, milliseconds);
  child.once('exit', onExit);
});
const profile = mkdtempSync(join(tmpdir(), 'kpdesk-tenant-browser-'));
const debugPort = await (async () => {
  const { createServer } = await import('node:net');
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
})();
const browser = spawn(chrome, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--ignore-certificate-errors', '--remote-allow-origins=*',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + debugPort,
  '--user-data-dir=' + profile, 'about:blank',
], { stdio: 'ignore' });
let ws;
try {
  let target;
  for (let i = 0; i < 80; i++) {
    const reply = await fetch('http://127.0.0.1:' + debugPort + '/json')
      .catch(() => null);
    if (reply?.ok) {
      target = (await reply.json()).find(item => item.type === 'page');
      if (target) break;
    }
    await sleep(100);
  }
  if (!target) throw new Error('Chrome CDP not available.');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let commandId = 0;
  const pending = new Map();
  ws.addEventListener('message', event => {
    const payload = JSON.parse(String(event.data));
    if (!pending.has(payload.id)) return;
    const { resolve, reject } = pending.get(payload.id);
    pending.delete(payload.id);
    payload.error ? reject(Error(payload.error.message)) : resolve(payload.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++commandId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (result.exceptionDetails) throw Error(
      result.exceptionDetails.text + ' ' +
      (result.exceptionDetails.exception?.description ?? ''),
    );
    return result.result.value;
  };
  const until = async (expression, label, timeout = 16000) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      if (await evaluate(expression).catch(() => false)) return;
      await sleep(150);
    }
    const page = await evaluate('document.body?.innerText?.slice(0,2200)');
    const state = await evaluate(`(() => ({
      request: document.querySelector('textarea[maxlength="12000"]')?.value,
      rows: [...document.querySelectorAll('.tenant-quote-line')].map(row => ({
        category: row.querySelector('select')?.value,
        quantity: row.querySelector('input[type=number]')?.value,
        reviewed: row.querySelector('.tenant-extracted-review input[type=checkbox]')?.checked,
        description: row.querySelector('input[maxlength="500"]')?.value,
      })),
    }))()`);
    throw Error(label + ' timed out. Browser page: ' + String(page)
      + ' Editor state: ' + JSON.stringify(state));
  };
  const change = async (selector, value, elementType = 'HTMLInputElement') => {
    const ok = await evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) return false;
      const prototype = ${elementType}.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(
        element, ${JSON.stringify(value)});
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    })()`);
    if (!ok) throw Error('Form control missing: ' + selector);
  };
  const click = async expression => {
    if (!(await evaluate(expression))) throw Error('Browser action unavailable: ' + expression);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: false,
  });
  await command('Page.navigate', { url: url + '/auth/login' });
  await until('!!document.querySelector("input[type=email]")', 'Login screen');
  await change('input[type=email]', email);
  await change('input[type=password]', password);
  await click('(() => {document.querySelector("button[type=submit]")?.click();return true})()');
  await until('location.pathname === "/app" && !!document.querySelector("input[type=file]")',
    'Authenticated organization screen');
  const org = await evaluate(`(async () => {
    const response = await fetch('/api/session/organizations');
    const payload = await response.json();
    if (!response.ok || payload.organizations?.length !== 1) throw Error('Expected one isolated tenant.');
    return payload.organizations[0].id;
  })()`);
  console.log('HTTPS session login + organization scoped UI: PASS');

  if (process.env.KP_DESK_E2E_LARGE_REVIEW === '1' || process.env.KP_DESK_E2E_BATCH_ONLY === '1') {
    await click(`(() => {
      const link = document.querySelector('a[href="#workspace-sources"]');
      if (!link) return false;
      link.click(); return document.querySelector('#workspace-sources').open;
    })()`);
  }

  if (process.env.KP_DESK_E2E_LARGE_REVIEW === '1') {
    await verifySupplierLargeBrowser({ command, evaluate, until, click, url, org });
  } else if (process.env.KP_DESK_E2E_BATCH_ONLY === '1') {
    await verifySupplierBatchBrowser({ evaluate, until, click, change, org });
  } else if (process.env.KP_DESK_E2E_PORTFOLIO_SAMPLE === '1') {
    const artifact = process.env.KP_DESK_E2E_PORTFOLIO_PDF;
    if (!artifact) throw Error('Isolated portfolio PDF artifact path missing');
    const result = await verifyPortfolioBrowser({ evaluate, until, click, change, org });
    const bytes = Buffer.from(result.pdf_base64, 'base64');
    if (bytes.length !== result.pdf_bytes) throw Error('Portfolio PDF length mismatch');
    writeFileSync(artifact, bytes, {flag: 'wx', mode: 0o600});
  } else {
  await verifyCompanyBrowser({command,evaluate,until,click,change});
  const csv = [
    'sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;brand;paper_format;paper_gsm;pen_ink_color;pen_ink_type;attributes_json',
    'E2E-PAPER-A;Office paper A4 80;paper;пачка;1;429.90;;A4;80;;;{}',
    'E2E-PAPER-B;Office paper A4 80 second;paper;пачка;1;450.00;;A4;80;;;{}',
  ].join('\n') + '\n';
  const uploaded = await evaluate(`(() => {
    const input = document.querySelector('input[type=file]');
    if (!input) return false;
    const transfer = new DataTransfer();
    transfer.items.add(new File([${JSON.stringify(csv)}], 'synthetic-e2e.csv',
      {type:'text/csv'}));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!uploaded) throw Error('Catalog upload UI missing.');
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent.includes('Проверить прайс перед публикацией'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`!!document.querySelector('[aria-label="Результат проверки прайса"]')
    && document.querySelector('[aria-label="Результат проверки прайса"]')
      .innerText.includes('готов к публикации')`, 'Catalog preflight approved');
  await click(`(() => {
    const checkbox = [...document.querySelectorAll('label')]
      .find(item => item.textContent.includes('Я проверил файл и подтверждаю публикацию'))
      ?.querySelector('input[type=checkbox]');
    if (!checkbox || checkbox.checked || checkbox.disabled) return false;
    const bounds = checkbox.getBoundingClientRect();
    if (bounds.width > 24 || bounds.height > 24) {
      throw Error('Publication checkbox is stretched: '+bounds.width+'x'+bounds.height);
    }
    checkbox.click(); return true;
  })()`);
  await until(`[...document.querySelectorAll('button')].some(
    item => item.textContent.includes('Опубликовать прайс') && !item.disabled)`,
    'Confirmed catalog import ready');
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent.includes('Опубликовать прайс'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until('document.body.innerText.includes("Опубликовано товаров: 2")',
    'Catalog publication');
  console.log('HTTPS browser catalog import: PASS');
  if (process.env.KP_DESK_E2E_CAPTURE_WORKSPACE === '1') {
    await verifyWorkspaceLayout({command,evaluate});
  }

  const lineSelector = '.tenant-quote-line input[maxlength="500"]';
  const blankManualLine = await evaluate(`(() => {
    const row = document.querySelector('.tenant-quote-line');
    return row?.querySelector('select')?.value === 'unknown'
      && row.querySelector('input[type="number"][max="100000"]')?.value === '0'
      && row.querySelector('select[required]')?.value === ''
      && !row.querySelector('input[maxlength="30"]');
  })()`);
  if (!blankManualLine) throw Error('Manual row must start without presumed category, quantity, unit or paper facts.');
  await change(lineSelector, 'Office paper A4 80');
  await change('.tenant-quote-line select', 'paper', 'HTMLSelectElement');
  await change('.tenant-quote-line input[type="number"][max="100000"]', '1');
  await change('.tenant-quote-line select[required]', 'пачка', 'HTMLSelectElement');
  await change('.tenant-quote-line input[maxlength="30"]', 'A4');
  await change('.tenant-quote-line input[type="number"][max="1000"]', '');
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent === 'Проверить полноту заявки');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const panel = document.querySelector('section[aria-label="Полнота заявки"]');
    return panel?.querySelector('textarea')?.value.includes('плотность');
  })()`, 'source-bound clarification without an additional AI call');
  await change('.tenant-quote-line input[type="number"][max="1000"]', '80');
  await until(`!document.querySelector('section[aria-label="Полнота заявки"] textarea')`,
    'editing a requirement invalidates the old analysis');
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent === 'Подобрать товары по всей заявке');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`document.body.innerText.includes('Подбор завершён для 1 позиций')
    && !!document.querySelector('.tenant-ml-results select')`, 'whole-request TF-IDF preview');
  if (await evaluate(`document.querySelector('.tenant-ml-results select').value !== ''`)) {
    throw Error('Batch preview must not silently confirm an SKU');
  }
  await click(`(() => {
    const button = [...document.querySelectorAll('.tenant-quote-line button')]
      .find(item => item.textContent.includes('Найти SKU (ML)'));
    const tools = button?.closest('details');
    if (tools && !tools.open) tools.querySelector('summary')?.click();
    if (!button || button.disabled) return false;
    button.click();return true;
  })()`);
  await until(`!!document.querySelector('.tenant-ml-results select')
    && document.querySelector('.tenant-ml-results').innerText.includes('модель ML')`,
    'ML candidate list replaces the earlier TF-IDF preview');
  const skuExists = await evaluate(`[...document.querySelectorAll('.tenant-ml-results option')]
    .some(option => option.value === 'E2E-PAPER-B')`);
  if (!skuExists) throw Error('ML preview did not return tenant SKU.');
  await change('.tenant-ml-results select', 'E2E-PAPER-B', 'HTMLSelectElement');
  await until(`!!document.querySelector('.tenant-ml-results .tenant-requirements')`,
    'selected SKU displays the evidence matrix');
  if (process.env.KP_DESK_E2E_CAPTURE_ANALYSIS === '1') {
    await evaluate(`(() => {
      const matrix = document.querySelector('.tenant-ml-results .tenant-requirements');
      matrix.open = true; matrix.scrollIntoView({block:'start',behavior:'instant'}); return true;
    })()`);
    for (const [label, width, height] of [['desktop', 1280, 900], ['mobile', 390, 844]]) {
      await command('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => {
        document.querySelector('.tenant-ml-results .tenant-requirements').scrollIntoView({block:'start',behavior:'instant'});
        resolve(true);
      }))`);
      const shot = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
      const layout = await evaluate(`(() => {
        const matrix = document.querySelector('.tenant-ml-results .tenant-requirements');
        const box = matrix.getBoundingClientRect();
        const ancestors = []; let element = matrix;
        while (element) {
          const rect = element.getBoundingClientRect();
          ancestors.push({tag:element.tagName,className:element.className,left:rect.left,width:rect.width});
          element=element.parentElement;
        }
        return {viewport:innerWidth,pageWidth:document.documentElement.scrollWidth,
          matrix:{left:box.left,top:box.top,width:box.width},ancestors};
      })()`);
      if (layout.matrix.width > layout.ancestors[1].width + 1
        || layout.matrix.left < -1 || layout.matrix.left + layout.matrix.width > width + 1) {
        throw Error('Evidence matrix overflows the ' + label + ' viewport: ' + JSON.stringify(layout.matrix));
      }
      writeFileSync(new URL('../../artifacts/engineering-audit-20261028/requirements-' + label + '-layout.json', import.meta.url),
        JSON.stringify(layout,null,2));
      writeFileSync(new URL('../../artifacts/engineering-audit-20261028/requirements-' + label + '.png', import.meta.url),
        Buffer.from(shot.data, 'base64'));
    }
    await command('Emulation.clearDeviceMetricsOverride');
  }
  await click(`(() => {
    const button=[...document.querySelectorAll('button')]
      .find(item => item.textContent.includes('Сохранить состав'));
    if (!button || button.disabled) return false;
    button.click();return true;
  })()`);
  await until(`!!document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
    && document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
      .innerText.includes('SKU подтверждён менеджером')`, 'Persisted quote in UI');
  const stored = await evaluate(`(async () => {
    const response=await fetch('/api/session/organizations/' + ${JSON.stringify(org)} + '/quotes');
    const data=await response.json();
    if (!response.ok || !data.items?.length) throw Error('No saved quotes.');
    return data.items[0].quote_id;
  })()`);
  const pdf = await evaluate(`(async () => {
    const response = await fetch('/api/session/organizations/' + ${JSON.stringify(org)}
      + '/quotes/' + ${JSON.stringify(stored)} + '/pdf');
    const buf = await response.arrayBuffer();
    return {status:response.status, prefix:String.fromCharCode(
      ...new Uint8Array(buf.slice(0,5)))};
  })()`);
  if (pdf.status !== 200 || pdf.prefix !== '%PDF-') throw Error('Persisted PDF invalid.');
  const pdfLink = await evaluate(`document.querySelector(
    'section[aria-label="Текущее коммерческое предложение"] a[href$="/pdf?revision=1"]'
  )?.getAttribute('href') ?? ''`);
  if (pdfLink !== '/api/session/organizations/' + org +
      '/quotes/' + stored + '/pdf?revision=1') {
    throw Error('A65 UI PDF is not pinned to the saved revision: ' + pdfLink);
  }
  console.log('HTTPS ML → manual SKU → quote persistence → revision-pinned PDF: PASS');
  if (process.env.KP_DESK_E2E_EXPECT_LIVE_AUDIT === '1') {
    const decision = await evaluate(`(async () => {
      const response = await fetch(
        '/api/session/organizations/' + ${JSON.stringify(org)} +
        '/quotes/' + ${JSON.stringify(stored)} + '/manager-decisions/1');
      if (!response.ok) throw Error('Manager audit HTTP ' + response.status);
      return response.json();
    })()`);
    if (decision.items?.length !== 1 ||
        decision.items[0].sku !== 'E2E-PAPER-B' ||
        !decision.items[0].approved_by_user_id) {
      throw Error('Manager audit not bound to selected SKU and actor.');
    }
    console.log('PostgreSQL A44 manager decision attribution: PASS');
  }

  await click(`(() => {
    const button=[...document.querySelectorAll('button')]
      .find(item => item.textContent.trim() === 'Создать новое КП');
    if (!button) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const rows=[...document.querySelectorAll('.tenant-quote-line')];
    const original=document.querySelector('textarea[maxlength="12000"]');
    return !document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
      && rows.length === 1
      && rows[0].querySelector('input[maxlength="500"]')?.value === ''
      && original?.value === ''
      && !document.querySelector('.tenant-ml-results');
  })()`, 'Clean new quote form after saved document');
  console.log('HTTPS new quote resets previous editor and SKU selection: PASS');

  await click(`(() => {
    const button=[...document.querySelectorAll('button')]
      .find(item => item.textContent.trim() === 'Выйти');
    if (!button) return false;
    button.click();return true;
  })()`);
  await until('location.pathname === "/auth/login"', 'Logout');
  await change('input[type=email]', email);
  await change('input[type=password]', password);
  await click('(() => {document.querySelector("button[type=submit]").click();return true})()');
  await until('location.pathname === "/app"', 'Repeat login');
  await until(`[...document.querySelectorAll('.tenant-list li')].some(
    item => item.textContent.includes(${JSON.stringify(stored.slice(0,8))}))`, 'Reopened quote list');
  await click(`(() => {
    const item=[...document.querySelectorAll('.tenant-list li')]
      .find(node => node.textContent.includes(${JSON.stringify(stored.slice(0,8))}));
    const button=[...item?.querySelectorAll('button')??[]]
      .find(node=>node.textContent.includes('Открыть'));
    if (!button) return false;button.click();return true;
  })()`);
  await until(`!!document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
    && document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
      .innerText.includes('E2E-PAPER-B')`, 'Selected SKU after repeat login');
  console.log('HTTPS logout → repeat login → saved quote reopen: PASS');
  if (process.env.KP_DESK_E2E_SIMULATE_GROQ_TIMEOUT === '1') {
    await change('textarea[maxlength="12000"]',
      '2 пачки бумаги A4 80 г/м²\n3 пачки бумаги A4 80 г/м²', 'HTMLTextAreaElement');
    await click(`(() => {
      const label=[...document.querySelectorAll('label')]
        .find(node => node.textContent.includes('Я подтверждаю право передать'));
      const input=label?.querySelector('input[type=checkbox]');
      if (!input) return false;
      input.click(); return input.checked;
    })()`);
    await click(`(() => {
      const button=[...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Извлечь позиции для проверки'));
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`document.body.innerText.includes(
      'Groq недоступен. Текст заявки разбит на строки локально.')`,
      'Groq timeout manual fallback notice');
    await until(`(() => {
      const rows=[...document.querySelectorAll('.tenant-quote-line')];
      return rows.length === 2
        && rows.every(row => row.querySelector('select')?.value === 'unknown')
        && rows.every(row => row.querySelector('input[type=number]')?.value === '0')
        && rows.every(row => !!row.querySelector(
          '.tenant-extracted-review input[type=checkbox]:not(:checked)'));
    })()`, 'Groq manual fallback editor rendered');
    const safeFallback = await evaluate(`(() => {
      const rows=[...document.querySelectorAll('.tenant-quote-line')];
      const categories=rows.map(row => row.querySelector('select')?.value);
      const quantities=rows.map(row =>
        row.querySelector('input[type=number]')?.value);
      const requiresReview=rows.every(row =>
        !!row.querySelector('.tenant-extracted-review input[type=checkbox]:not(:checked)'));
      const save=[...document.querySelectorAll('button')].find(button =>
        button.textContent.includes('Сохранить состав'));
      return {
        count: rows.length, categories, quantities, requiresReview,
        saveDisabled: save?.disabled, descriptions: rows.map(row =>
          row.querySelector('input[maxlength="500"]')?.value),
      };
    })()`);
    if (!(safeFallback.count === 2 &&
      safeFallback.categories.every(cat => cat === 'unknown') &&
      safeFallback.quantities.every(qty => qty === '0') &&
      safeFallback.requiresReview && safeFallback.saveDisabled)) {
      throw Error('Groq timeout permitted unsafe fallback: ' + JSON.stringify(safeFallback));
    }
    console.log('A48 Groq timeout → safe manual-only rows: PASS');

    for (const [index, quantity, sku] of [
      [0, 2, 'E2E-PAPER-A'], [1, 3, 'E2E-PAPER-B'],
    ]) {
      const position = index + 1;
      const fieldset = '.tenant-quote-line:nth-of-type(' + position + ')';
      await change(fieldset + ' select', 'paper', 'HTMLSelectElement');
      await change(fieldset + ' input[type="number"][max="100000"]', String(quantity));
      await change(fieldset + ' select[required]', 'пачка', 'HTMLSelectElement');
      await change(fieldset + ' input[maxlength="30"]', 'A4');
      await change(fieldset + ' input[type="number"][max="1000"]', '80');
      await click(`(() => {
        const row = document.querySelectorAll('.tenant-quote-line')[${index}];
        const button = [...row?.querySelectorAll('button') ?? []]
          .find(item => item.textContent.includes('Найти SKU (ML)'));
    const tools = button?.closest('details');
    if (tools && !tools.open) tools.querySelector('summary')?.click();
        if (!button || button.disabled) return false;
        button.click(); return true;
      })()`);
      await until(`!!document.querySelector(
        ${JSON.stringify(fieldset + ' .tenant-ml-results select')})`,
        'A59 ML candidates for position ' + position);
      const compatible = await evaluate(`[...document.querySelectorAll(
        ${JSON.stringify(fieldset + ' .tenant-ml-results option')})]
        .some(item => item.value === ${JSON.stringify(sku)} && !item.disabled)`);
      if (!compatible) throw Error('A59 expected compatible SKU missing: ' + sku);
      await change(fieldset + ' .tenant-ml-results select', sku, 'HTMLSelectElement');
      await click(`(() => {
        const box = document.querySelector(
          ${JSON.stringify(fieldset + ' .tenant-extracted-review input[type="checkbox"]')});
        if (!box || box.checked) return false;
        box.click(); return box.checked;
      })()`);
      const buttonState = await evaluate(`[...document.querySelectorAll('button')]
        .find(button => button.textContent.includes('Сохранить состав'))?.disabled`);
      if (buttonState !== (index === 0)) {
        throw Error('A59 per-position human review gate invalid');
      }
    }
    await verifyVariantsBrowser({ command, evaluate, until, click, change });
    await click(`(() => {
      const save = [...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Сохранить состав'));
      if (!save || save.disabled) return false;
      save.click(); return true;
    })()`);
    await until(`(() => {
      const section = document.querySelector(
        'section[aria-label="Текущее коммерческое предложение"]');
      return !!section
        && section.querySelectorAll(':scope > ul.tenant-list > li').length === 2
        && section.innerText.includes('E2E-PAPER-A')
        && section.innerText.includes('E2E-PAPER-B');
    })()`, 'A59 two-row persisted quote rendered');
    const multiline = await evaluate(`(async () => {
      const prefix = '/api/session/organizations/' + ${JSON.stringify(org)} + '/quotes';
      const response = await fetch(prefix);
      const listed = await response.json();
      if (!response.ok || listed.items?.length !== 2) {
        throw Error('A59 two independent documents not persisted');
      }
      const entry = listed.items.find(item => item.quote_id !== ${JSON.stringify(stored)});
      if (!entry || entry.total_rub !== '2209.80' || !entry.ready_for_draft) {
        throw Error('A59 new quote total or readiness invalid');
      }
      const detailResponse = await fetch(prefix + '/' + entry.quote_id);
      const quote = await detailResponse.json();
      if (!detailResponse.ok || quote.approved || quote.lines?.length !== 2
          || quote.revision !== 1 || quote.total_rub !== '2209.80') {
        throw Error('A59 persisted quote snapshot invalid');
      }
      const expected = [
        ['E2E-PAPER-A', '2', '429.90', '859.80'],
        ['E2E-PAPER-B', '3', '450.00', '1350.00'],
      ];
      for (const [index, [sku, quantity, price, sum]] of expected.entries()) {
        const line = quote.lines[index];
        if (line.sku !== sku || line.sale_quantity !== Number(quantity)
            || line.unit_price_rub !== price || line.line_total_rub !== sum
            || line.status !== 'manager_selected') {
          throw Error('A59 line discrepancy at index ' + index + ': '
            + JSON.stringify(line));
        }
      }
      const pdf = await fetch(prefix + '/' + entry.quote_id + '/pdf');
      const bytes = new Uint8Array(await pdf.arrayBuffer());
      if (pdf.status !== 200 || String.fromCharCode(...bytes.slice(0, 5)) !== '%PDF-') {
        throw Error('A59 multi-position PDF unavailable');
      }
      return entry.quote_id;
    })()`);
    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.trim() === 'Выйти');
      if (!button) return false;
      button.click(); return true;
    })()`);
    await until('location.pathname === "/auth/login"', 'A59 logout');
    await change('input[type=email]', email);
    await change('input[type=password]', password);
    await click('(() => {document.querySelector("button[type=submit]").click();return true})()');
    await until('location.pathname === "/app"', 'A59 repeat login');
    await until(`[...document.querySelectorAll('.tenant-list li')].some(
      item => item.textContent.includes(${JSON.stringify(multiline.slice(0, 8))}))`,
      'A59 persisted multi-line quote in history');
    await click(`(() => {
      const item = [...document.querySelectorAll('.tenant-list li')]
        .find(node => node.textContent.includes(${JSON.stringify(multiline.slice(0, 8))}));
      const button = [...item?.querySelectorAll('button') ?? []]
        .find(node => node.textContent.includes('Открыть'));
      if (!button) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const section = document.querySelector(
        'section[aria-label="Текущее коммерческое предложение"]');
      return !!section
        && section.querySelectorAll(':scope > ul.tenant-list > li').length === 2
        && section.innerText.includes('E2E-PAPER-A')
        && section.innerText.includes('E2E-PAPER-B');
    })()`, 'A59 reopened multi-position quote');
    console.log('A59 two-row human review → distinct SKU → 2209.80 RUB → PDF → reopen: PASS');

    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.trim() === 'Создать новое КП');
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await change('textarea[maxlength="12000"]',
      '2 пачки бумаги A4 80 г/м²\n3 пачки бумаги A4, плотность уточняется',
      'HTMLTextAreaElement');
    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Разбить строки вручную (без Groq)'));
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const rows = [...document.querySelectorAll('.tenant-quote-line')];
      return rows.length === 2 && rows.every(row =>
        row.querySelector('select')?.value === 'unknown');
    })()`, 'A60 manual split with unresolved paper specification');

    for (const [index, gsm] of [[0, 80], [1, 120]]) {
      const selector = '.tenant-quote-line:nth-of-type(' + (index + 1) + ')';
      await change(selector + ' select', 'paper', 'HTMLSelectElement');
      await change(selector + ' input[type="number"][max="100000"]',
        String(index === 0 ? 2 : 3));
      await change(selector + ' select[required]', 'пачка', 'HTMLSelectElement');
      await change(selector + ' input[maxlength="30"]', 'A4');
      await change(selector + ' input[type="number"][max="1000"]', String(gsm));
      await click(`(() => {
        const row = document.querySelectorAll('.tenant-quote-line')[${index}];
        const button = [...row?.querySelectorAll('button') ?? []]
          .find(item => item.textContent.includes('Найти SKU (ML)'));
    const tools = button?.closest('details');
    if (tools && !tools.open) tools.querySelector('summary')?.click();
        if (!button || button.disabled) return false;
        button.click(); return true;
      })()`);
      await until(`!!document.querySelector(
        ${JSON.stringify(selector + ' .tenant-ml-results')})`,
        'A60 ML preview row ' + (index + 1));
      if (index === 0) {
        await change(selector + ' .tenant-ml-results select',
          'E2E-PAPER-A', 'HTMLSelectElement');
      } else {
        const incompatible = await evaluate(`(() => {
          const row = document.querySelectorAll('.tenant-quote-line')[1];
          const selector = row.querySelector('.tenant-ml-results select');
          const compatible = selector ? [...selector.options]
            .filter(option => option.value && !option.disabled) : [];
          return compatible.length === 0;
        })()`);
        if (!incompatible) throw Error('A60 120gsm must not admit 80gsm catalog SKU');
      }
      await click(`(() => {
        const box = document.querySelector(
          ${JSON.stringify(selector + ' .tenant-extracted-review input[type="checkbox"]')});
        if (!box || box.checked) return false;
        box.click(); return box.checked;
      })()`);
    }
    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Сохранить состав'));
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const section = document.querySelector(
        'section[aria-label="Текущее коммерческое предложение"]');
      return !!section
        && section.innerText.includes('E2E-PAPER-A')
        && section.querySelectorAll(':scope > ul.tenant-list > li').length === 2
        && !section.querySelector('a[href*="/pdf?revision="]');
    })()`, 'A60 incomplete quote has no draft PDF link');
    const pendingQuote = await evaluate(`(async () => {
      const prefix = '/api/session/organizations/' + ${JSON.stringify(org)} + '/quotes';
      const listed = await (await fetch(prefix)).json();
      if (listed.items?.length !== 3) throw Error('A60 expected third isolated quote');
      const current = listed.items.filter(item =>
        item.total_rub === null && item.ready_for_draft === false);
      if (current.length !== 1) throw Error('A60 missing incomplete document');
      const id = current[0].quote_id;
      const response = await fetch(prefix + '/' + id);
      const quote = await response.json();
      if (!response.ok || quote.revision !== 1 || quote.lines.length !== 2
          || quote.ready_for_draft || quote.total_rub !== null
          || quote.lines[0].status !== 'manager_selected'
          || quote.lines[0].sku !== 'E2E-PAPER-A'
          || quote.lines[0].line_total_rub !== '859.80'
          || !['needs_review', 'no_match'].includes(quote.lines[1].status)
          || quote.lines[1].sku !== null) {
        throw Error('A60 unsafe incomplete snapshot ' + JSON.stringify(quote));
      }
      const pdf = await fetch(prefix + '/' + id + '/pdf');
      const failure = await pdf.json();
      if (pdf.status !== 409 || failure.code !== 'incomplete_draft') {
        throw Error('A60 incomplete draft incorrectly exposed PDF');
      }
      return id;
    })()`);
    const savedRow = await evaluate(`(() => {
      const row = document.querySelectorAll('.tenant-quote-line')[0];
      return {
        sku: row?.querySelector('.tenant-ml-results select')?.value,
        approved: row?.querySelector('.tenant-extracted-review input[type="checkbox"]')?.checked,
      };
    })()`);
    if (savedRow.sku !== 'E2E-PAPER-A' || savedRow.approved !== true) {
      throw Error('A60 valid first row lost during incomplete save');
    }
    const second = '.tenant-quote-line:nth-of-type(2)';
    await change(second + ' input[type="number"][max="1000"]', '80');
    await change(second + ' input[maxlength="500"]',
      '3 пачки бумаги A4 80 г/м² после уточнения');
    const firstStillApproved = await evaluate(`(() => {
      const row = document.querySelectorAll('.tenant-quote-line')[0];
      const first = row.querySelector('.tenant-ml-results select');
      const saved = row.querySelector('.tenant-extracted-review input[type="checkbox"]');
      const changed = document.querySelectorAll('.tenant-quote-line')[1]
        .querySelector('.tenant-extracted-review input[type="checkbox"]');
      return first?.value === 'E2E-PAPER-A' && saved?.checked && !changed?.checked;
    })()`);
    if (!firstStillApproved) throw Error('A60 edit invalidated unaffected manager selection');
    await click(`(() => {
      const row = document.querySelectorAll('.tenant-quote-line')[1];
      const button = [...row?.querySelectorAll('button') ?? []]
        .find(item => item.textContent.includes('Найти SKU (ML)'));
    const tools = button?.closest('details');
    if (tools && !tools.open) tools.querySelector('summary')?.click();
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`!!document.querySelector(
      ${JSON.stringify(second + ' .tenant-ml-results select')})`,
      'A60 repaired row returns compatible candidate');
    await change(second + ' .tenant-ml-results select',
      'E2E-PAPER-B', 'HTMLSelectElement');
    await click(`(() => {
      const box = document.querySelector(
        ${JSON.stringify(second + ' .tenant-extracted-review input[type="checkbox"]')});
      if (!box || box.checked) return false;
      box.click(); return box.checked;
    })()`);
    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Сохранить состав'));
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const section = document.querySelector(
        'section[aria-label="Текущее коммерческое предложение"]');
      return !!section
        && section.querySelectorAll(':scope > ul.tenant-list > li').length === 2
        && section.innerText.includes('E2E-PAPER-A')
        && section.innerText.includes('E2E-PAPER-B')
        && !!section.querySelector('a[href$="/pdf?revision=2"]')
        && section.querySelector('h2')?.innerText.includes('ревизия 2');
    })()`, 'A60 completed revision after targeted row repair');
    const restored = await evaluate(`(async () => {
      const prefix = '/api/session/organizations/' + ${JSON.stringify(org)} + '/quotes/';
      const response = await fetch(prefix + ${JSON.stringify(pendingQuote)});
      const quote = await response.json();
      if (!response.ok || quote.revision !== 2 || !quote.ready_for_draft
          || quote.total_rub !== '2209.80' || quote.lines.length !== 2
          || quote.lines[0].sku !== 'E2E-PAPER-A'
          || quote.lines[1].sku !== 'E2E-PAPER-B'
          || quote.lines.some(line => line.status !== 'manager_selected')) {
        throw Error('A60 repaired snapshot invalid: ' + JSON.stringify(quote));
      }
      const oldPdf = await fetch(prefix + ${JSON.stringify(pendingQuote)}
        + '/pdf?revision=1');
      const newPdf = await fetch(prefix + ${JSON.stringify(pendingQuote)} + '/pdf');
      const bytes = new Uint8Array(await newPdf.arrayBuffer());
      if (oldPdf.status !== 409 || newPdf.status !== 200
          || String.fromCharCode(...bytes.slice(0, 5)) !== '%PDF-') {
        throw Error('A60 historical incomplete PDF should stay blocked');
      }
      return quote.revision;
    })()`);
    if (restored !== 2) throw Error('A60 revision did not advance');
    console.log('A60 partial quote PDF blocked → repair one line → original SKU retained → PDF: PASS');
    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.includes('Открыть предыдущую ревизию'));
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const old = document.querySelector('section[aria-label="Историческая ревизия КП"]');
      const latest = document.querySelector('section[aria-label="Текущее коммерческое предложение"]');
      return !!old && old.innerText.includes('Ревизия 1')
        && old.innerText.includes('PDF недоступен')
        && !old.querySelector('a[href*="/pdf"]')
        && !!latest?.querySelector('a[href$="/pdf?revision=2"]')
        && latest.querySelector('h2')?.innerText.includes('ревизия 2');
    })()`, 'A65 read-only previous revision, incomplete old PDF blocked');
    console.log('A65 historical revision read-only, old incomplete PDF withheld, current PDF pinned: PASS');

    await click(`(() => {
      const button = [...document.querySelectorAll('button')]
        .find(item => item.textContent.trim() === 'Выйти');
      if (!button) return false;
      button.click(); return true;
    })()`);
    await until('location.pathname === "/auth/login"', 'A61 logout before restore');
    await change('input[type=email]', email);
    await change('input[type=password]', password);
    await click('(() => {document.querySelector("button[type=submit]").click();return true})()');
    await until('location.pathname === "/app"', 'A61 repeat login');
    await until(`[...document.querySelectorAll('.tenant-list li')].some(
      item => item.textContent.includes(${JSON.stringify(pendingQuote.slice(0, 8))}))`,
      'A61 saved quote visible');
    await click(`(() => {
      const item = [...document.querySelectorAll('.tenant-list li')]
        .find(node => node.textContent.includes(${JSON.stringify(pendingQuote.slice(0, 8))}));
      const button = [...item?.querySelectorAll('button') ?? []]
        .find(node => node.textContent.includes('Открыть'));
      if (!button) return false;
      button.click(); return true;
    })()`);
    await until(`(() => {
      const rows = [...document.querySelectorAll('.tenant-quote-line')];
      return rows.length === 2
        && rows[0].querySelector('input[maxlength="500"]')?.value.includes('бумаги A4')
        && rows[1].querySelector('input[maxlength="500"]')?.value.includes('уточнения')
        && rows[0].querySelector('input[type="number"][max="100000"]')?.value === '2'
        && rows[1].querySelector('input[type="number"][max="100000"]')?.value === '3'
        && rows.every(row => row.querySelector('select')?.value === 'paper')
        && rows.every(row => row.querySelector('input[type="number"][max="1000"]')?.value === '80')
        && rows.every(row => !row.querySelector('.tenant-ml-results select'))
        && rows.every(row => !!row.querySelector(
          '.tenant-extracted-review input[type="checkbox"]:not(:checked)'))
        && !![...document.querySelectorAll('button')].find(button =>
          button.textContent.includes('Сохранить состав') && button.disabled);
    })()`, 'A61 safely recovered unapproved editable quote input');
    console.log('A61 reopen restores typed request but requires fresh per-line review and SKU: PASS');
    await verifyProposalBrowser({ command, evaluate, until, click });
  }
  }
} finally {
  // Chrome renderer processes retain profile handles on Windows after kill().
  // Prefer a graceful CDP shutdown and await the child exit before removing
  // the profile. Keep a bounded forced-shutdown path and surface cleanup errors.
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ id: 0, method: 'Browser.close' }));
    if (!await exitedWithin(browser, 3000)) browser.kill();
  } else {
    browser.kill();
  }
  if (!await exitedWithin(browser, 3000)) {
    throw new Error('Chrome did not terminate; temporary profile remains in use.');
  }
  ws?.close();
  rmSync(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 200 });
}
