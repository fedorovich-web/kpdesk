// Isolated Chrome walkthrough of the exact downloadable synthetic portfolio CSV.
import { readFileSync } from 'node:fs';

const csv = readFileSync(new URL('../public/templates/kpdesk-portfolio-synthetic-v1.csv', import.meta.url), 'utf8');
if (Buffer.byteLength(csv) > 100_000 ||
    !csv.startsWith('sku;name;category;') ||
    !csv.includes('PAPER-A4-80') || !csv.includes('PAPER-A4-65')) {
  throw Error('Portfolio fixture drift or oversize');
}

export async function verifyPortfolioBrowser({ evaluate, until, change, click, org }) {
  // This function runs only inside the provenance-tagged PostgreSQL clone,
  // over the isolated HTTPS browser. No calls to outside providers.
  const uploaded = await evaluate(`(() => {
    const input = document.querySelector('input[type=file]');
    if (!input) return false;
    const transfer = new DataTransfer();
    transfer.items.add(new File([${JSON.stringify(csv)}],
      'kpdesk-portfolio-synthetic-v1.csv', {type:'text/csv'}));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
    return true;
  })()`);
  if (!uploaded) throw Error('Portfolio CSV input missing');
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent.includes('Проверить прайс перед публикацией'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const section = document.querySelector('[aria-label="Результат проверки прайса"]');
    return section?.innerText.includes('готов к публикации') &&
      section?.innerText.includes('5');
  })()`, 'Portfolio preflight of five synthetic rows');

  await click(`(() => {
    const checkbox = [...document.querySelectorAll('label')]
      .find(item => item.textContent.includes('Я проверил файл и подтверждаю публикацию'))
      ?.querySelector('input[type=checkbox]');
    if (!checkbox || checkbox.checked || checkbox.disabled) return false;
    checkbox.click(); return true;
  })()`);
  await click(`(() => {
    const button = [...document.querySelectorAll('button')]
      .find(item => item.textContent.includes('Опубликовать прайс'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until('document.body.innerText.includes("Опубликовано товаров: 5")',
    'Portfolio five-SKU catalog publication');

  const current = await evaluate(`(async () => {
    const path='/api/session/organizations/' + ${JSON.stringify(org)};
    const response=await fetch(path + '/catalog/current');
    const catalog=await response.json();
    if (response.status!==200 || catalog.rows_count!==5) {
      throw Error('Portfolio current catalog mismatch');
    }
    return catalog;
  })()`);

  const row = '.tenant-quote-line';
  const description = row + ' input[maxlength="500"]';
  if (await evaluate(`!!document.querySelector('section[aria-label="Утверждённое предложение"]')`)) {
    await click(`(() => {
      const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'Изменить состав');
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
  }
  if (await evaluate(`!!document.querySelector('section[aria-label="Текущее коммерческое предложение"]')`)) {
    await click(`(() => {
      const button = [...document.querySelectorAll('button')].find(item => item.textContent.trim() === 'Создать новое КП');
      if (!button || button.disabled) return false;
      button.click(); return true;
    })()`);
    await until(`!document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
      && document.querySelectorAll('.tenant-quote-line').length === 1`, 'Portfolio uses an independent new quote');
  }
  // Deliberately request a product absent from the catalog.
  await change(description, 'Бумага офисная A4 120 г/м2');
  await change(row + ' select', 'paper', 'HTMLSelectElement');
  await change(row + ' input[maxlength="30"]', 'A4');
  await change(row + ' input[type="number"][max="100000"]', '2');
  await change(row + ' select[required]', 'пачка', 'HTMLSelectElement');
  await change(row + ' input[type="number"][max="1000"]', '120');
  await click(`(() => {
    const button = [...document.querySelectorAll('.tenant-quote-line button')]
      .find(item => item.textContent.includes('Найти SKU (ML)'));
    if (!button || button.disabled) return false;
    const tools = button.closest('details');
    if (tools && !tools.open) tools.querySelector('summary').click();
    button.click();return true;
  })()`);
  await until(`!!document.querySelector('.tenant-ml-results')`,
    'Portfolio no-match preview');
  const unsafe = await evaluate(`(() => {
    const area=document.querySelector('.tenant-ml-results');
    return [...area?.querySelectorAll('option')??[]]
      .filter(node => node.value && !node.disabled)
      .map(node => node.value);
  })()`);
  if (unsafe.length) throw Error('Incompatible paper was offered: ' + unsafe);

  // Correct the request and require the exact SKU, not a similar one.
  await change(description, 'Бумага офисная А4 80 г/м2 500 листов');
  await change(row + ' input[type="number"][max="1000"]', '80');
  await change(row + ' input[type="number"][max="100000"]', '2');
  await until(`!document.querySelector('.tenant-ml-results')`,
    'Stale candidate list invalidated on request change');
  await click(`(() => {
    const button=[...document.querySelectorAll('.tenant-quote-line button')]
      .find(item => item.textContent.includes('Найти SKU (ML)'));
    if (!button || button.disabled) return false;
    const tools = button.closest('details');
    if (tools && !tools.open) tools.querySelector('summary').click();
    button.click(); return true;
  })()`);
  await until(`!!document.querySelector('.tenant-ml-results select')`,
    'Matching SKU for corrected portfolio request');
  const actualCandidates = await evaluate(`[...document.querySelectorAll('.tenant-ml-results option')]
    .filter(option => option.value && !option.disabled).map(option => option.value)`);
  if (JSON.stringify(actualCandidates) !== JSON.stringify(['PAPER-A4-80'])) {
    throw Error('Unsafe or missing portfolio candidates: ' + JSON.stringify(actualCandidates));
  }
  await change('.tenant-ml-results select', 'PAPER-A4-80', 'HTMLSelectElement');
  await click(`(() => {
    const save=[...document.querySelectorAll('button')]
      .find(button => button.textContent.includes('Сохранить состав'));
    if (!save || save.disabled) return false;
    save.click(); return true;
  })()`);
  await until(`(() => {
    const section=document.querySelector('section[aria-label="Текущее коммерческое предложение"]');
    return section?.innerText.includes('798.00') &&
      section?.innerText.includes('PAPER-A4-80') &&
      !!section?.querySelector('a[href*="/pdf?revision="]');
  })()`, 'Portfolio saved quote and revision-pinned PDF link');

  const result = await evaluate(`(async () => {
    const base='/api/session/organizations/'+${JSON.stringify(org)};
    const listing=await (await fetch(base+'/quotes')).json();
    const matching=listing.items?.filter(item=>item.total_rub==='798.00'&&item.ready_for_draft);
    if (matching?.length!==1) throw Error('Expected exactly one portfolio quote');
    const entry=matching[0];
    const response=await fetch(base+'/quotes/'+entry.quote_id);
    const quote=await response.json();
    if (!response.ok || quote.catalog_version_id!==${JSON.stringify(current.version_id)}
        || quote.revision!==1 || quote.approved!==false
        || quote.lines?.length!==1
        || quote.lines[0].sku!=='PAPER-A4-80'
        || quote.lines[0].unit_price_rub!=='399.00'
        || quote.lines[0].line_total_rub!=='798.00') {
      throw Error('Saved portfolio quote diverges from the CSV');
    }
    const pdf=await fetch(base+'/quotes/'+entry.quote_id+'/pdf?revision=1');
    const bytes=new Uint8Array(await pdf.arrayBuffer());
    if (pdf.status!==200 || String.fromCharCode(...bytes.slice(0,5))!=='%PDF-'
        || bytes.length > 500000 || bytes.length < 100) {
      throw Error('Portfolio PDF bytes missing or invalid size');
    }
    let binary='';
    for (let i=0;i<bytes.length;i+=8192) {
      binary+=String.fromCharCode(...bytes.slice(i,i+8192));
    }
    return {quote_id: entry.quote_id, revision: quote.revision, total:quote.total_rub,
      sku:quote.lines[0].sku, pdf_bytes:bytes.length,
      catalog_version_id: quote.catalog_version_id, pdf_base64:btoa(binary)};
  })()`);
  console.log('PORTFOLIO HTTPS CSV → 120gsm abstain → 80gsm ML → 798 RUB quote → PDF: PASS');
  return result;
}
