import { writeFileSync } from 'node:fs';
import { verifyTrainingFeedbackBrowser } from './tenant-training-feedback-browser-steps.mjs';

/** Real React controls, session API, provider adapter and immutable document. */
export async function verifyProposalBrowser({ command, evaluate, until, click }) {
  if (process.env.KP_DESK_E2E_TRAINING_FEEDBACK === '1') {
    await verifyTrainingFeedbackBrowser({ command, evaluate, until, click });
  }
  const selector = 'section[aria-label="ИИ-генератор текста КП"]';
  await until(`!!document.querySelector(${JSON.stringify(selector)})`, 'AI proposal composer is available');
  await until(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    return section.innerText.includes('Используются сведения компании, версия 1')
      && section.querySelector('input[maxlength="200"]').value === 'Учебная компания';
  })()`, 'saved company facts and sender are applied before generation');
  await click(`(() => {
    const details = document.querySelector(${JSON.stringify(selector + ' details.company-recipient-lookup')});
    if (!details || details.open) return false;
    details.querySelector('summary').click(); return details.open;
  })()`);
  await evaluate(`(() => {
    const input = document.querySelector(${JSON.stringify(selector + ' .company-recipient-lookup input[maxlength="12"]')});
    if (!input || input.disabled) throw Error('Recipient INN input is unavailable');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '7736207543');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`);
  await click(`(() => {
    const details = document.querySelector(${JSON.stringify(selector + ' .company-recipient-lookup')});
    const button = [...details.querySelectorAll('button')].find(item => item.textContent === 'Найти по ИНН');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const preview = section.querySelector('.company-recipient-lookup .company-inn-summary');
    const recipient = [...section.querySelectorAll('label')].find(label => label.textContent.trim() === 'Для кого предложение')?.querySelector('input');
    return preview?.innerText.includes('Учебный заказчик') && preview.innerText.includes('7736207543')
      && recipient?.value === '';
  })()`, 'synthetic registry response stays a preview until explicit recipient Apply');
  await click(`(() => {
    const details = document.querySelector(${JSON.stringify(selector + ' .company-recipient-lookup')});
    const button = [...details.querySelectorAll('button')].find(item => item.textContent === 'Применить реквизиты');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const recipient = [...section.querySelectorAll('label')].find(label => label.textContent.trim() === 'Для кого предложение')?.querySelector('input');
    const requisites = section.querySelector('[aria-label="Реквизиты заказчика"]');
    return recipient?.value === 'Учебный заказчик' && requisites?.innerText.includes('7736207543');
  })()`, 'recipient name and registry requisites are deliberately added to the proposal draft');
  await click(`(() => {
    const label=[...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('label')]
      .find(el=>el.textContent.includes('Краткое предложение'));
    if (!label?.querySelector('input')) return false;
    label.querySelector('input').click();return true;
  })()`);
  await evaluate(`(() => {
    const input=document.querySelector(${JSON.stringify(selector+' input[type="color"]')});
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'#1d4ed8');
    input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true;
  })()`);
  if (process.env.KP_DESK_E2E_CAPTURE_TEMPLATES==='1') {
    for (const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>{
        const el=document.querySelector('.document-appearance');
        scrollTo({top:scrollY+el.getBoundingClientRect().top,left:0,behavior:'instant'});
        requestAnimationFrame(()=>resolve(true));
      }))`);
      const fits=await evaluate(`document.documentElement.scrollWidth<=innerWidth`);
      if (!fits) throw Error('Document template controls overflow');
      const shot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(new URL('../../artifacts/pdf-brand-v2-20261030/document-template-'+name+'.png',import.meta.url),Buffer.from(shot.data,'base64'));
    }
  }
  await click(`(() => {
    const button=[...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
      .find(el=>el.textContent==='Найти дополнительные составы для КП');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  const choices='section[aria-label="Дополнительные варианты в клиентском КП"]';
  await until(`!!document.querySelector(${JSON.stringify(choices+' .composition-card input')})`, 'saved request offers an additional real composition');
  await click(`(() => {document.querySelector(${JSON.stringify(choices+' .composition-card input')}).click();return true})()`);
  if (process.env.KP_DESK_E2E_CAPTURE_DOCUMENT_OPTIONS==='1') {
    for (const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>{
        const el=document.querySelector(${JSON.stringify(choices)});
        scrollTo({top:scrollY+el.getBoundingClientRect().top,left:0,behavior:'instant'});
        requestAnimationFrame(()=>resolve(true));
      }))`);
      const layout=await evaluate(`(() => ({width:innerWidth,page:document.documentElement.scrollWidth,
        cards:[...document.querySelectorAll(${JSON.stringify(choices+' .composition-card')})].map(el=>({scroll:el.scrollWidth,client:el.clientWidth}))}))()`);
      if (layout.page>width || layout.cards.some(el=>el.scroll>el.client+1)) throw Error('Document options overflow: '+JSON.stringify(layout));
      const shot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(new URL('../../artifacts/engineering-audit-20261028/document-options-'+name+'.png',import.meta.url),Buffer.from(shot.data,'base64'));
    }
  }
  await click(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const box = section?.querySelector('input[name="external-proposal-consent"]');
    if (!box || box.disabled) return false;
    if (!box.checked) box.click();
    return box.checked;
  })()`);
  await click(`(() => {
    const button = [...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
      .find(item => item.textContent.includes('Сгенерировать предложение с ИИ'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const fields = document.querySelectorAll('div[aria-label="Текст предложения от ИИ"] textarea');
    return fields.length === 4 && [...fields].every(field => field.value.trim().length > 0);
  })()`, 'AI returns substantive proposal text without manager authorship');
  await until(`!!document.querySelector('div[aria-label="Варианты для утверждения"] .composition-card')`, 'generated proposal includes selected alternative');
  const reviewBlocked = await evaluate(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const review = section.querySelector('div[aria-label="Текст предложения от ИИ"] input[type="checkbox"]');
    const approve = [...section.querySelectorAll('button')]
      .find(button => button.textContent.includes('Утвердить КП и подготовить PDF'));
    return review?.disabled && !review.checked && approve?.disabled
      && !section.querySelector('iframe[title="Предпросмотр коммерческого предложения"]');
  })()`);
  if (!reviewBlocked) throw Error('Approval and attestation must require the current client PDF preview.');
  await evaluate(`(() => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const loaded = event => {
      const frame = event.target;
      if (frame instanceof HTMLIFrameElement && frame.title === 'Предпросмотр коммерческого предложения'
        && frame.src.startsWith('blob:')) {
        frame.dataset.previewLoaded = 'true';
        section.removeEventListener('load', loaded, true);
      }
    };
    section.addEventListener('load', loaded, true);
    return true;
  })()`);
  await click(`(() => {
    const button = [...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
      .find(item => item.textContent === 'Посмотреть КП перед утверждением');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const frame = document.querySelector(${JSON.stringify(selector + ' iframe[title="Предпросмотр коммерческого предложения"]')});
    return frame?.src.startsWith('blob:') && frame.dataset.previewLoaded === 'true';
  })()`, 'real client PDF blob loads in the preview iframe before manager review');
  const realPreview = await evaluate(`(async () => {
    const section = document.querySelector(${JSON.stringify(selector)});
    const frame = section.querySelector('iframe[title="Предпросмотр коммерческого предложения"]');
    const response = await fetch(frame.src);
    const bytes = new Uint8Array(await response.arrayBuffer());
    const review = section.querySelector('div[aria-label="Текст предложения от ИИ"] input[type="checkbox"]');
    return response.ok && response.headers.get('content-type')?.startsWith('application/pdf')
      && bytes.length > 5 && String.fromCharCode(...bytes.slice(0, 5)) === '%PDF-'
      && !review.disabled && !review.checked;
  })()`);
  if (!realPreview) throw Error('Client preview must be an actual loaded PDF and enable explicit review.');
  if (process.env.KP_DESK_E2E_CAPTURE_PREVIEW === '1') {
    const originalViewport = await evaluate('({ width: innerWidth, height: innerHeight })');
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await command('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => {
        const section = document.querySelector(${JSON.stringify(selector + ' .proposal-pdf-preview')});
        section.scrollIntoView({ block: 'start', behavior: 'instant' });
        setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))), 500);
      }))`);
      const layout = await evaluate(`(() => {
        const section = document.querySelector(${JSON.stringify(selector + ' .proposal-pdf-preview')});
        const frame = section.querySelector('iframe');
        const bounds = frame.getBoundingClientRect();
        return { viewport: innerWidth, page: document.documentElement.scrollWidth,
          sectionScroll: section.scrollWidth, sectionClient: section.clientWidth,
          frame: { left: bounds.left, right: bounds.right, width: bounds.width },
          loaded: frame.dataset.previewLoaded === 'true' && frame.src.startsWith('blob:') };
      })()`);
      if (!layout.loaded || layout.page > width || layout.sectionScroll > layout.sectionClient + 1
        || layout.frame.left < -1 || layout.frame.right > width + 1 || layout.frame.width <= 0) {
        throw Error('Client PDF preview fails ' + name + ' layout: ' + JSON.stringify(layout));
      }
      const shot = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
      writeFileSync(new URL('../../artifacts/engineering-audit-20261028/proposal-preview-' + name + '.png', import.meta.url),
        Buffer.from(shot.data, 'base64'));
    }
    await command('Emulation.setDeviceMetricsOverride', { ...originalViewport, deviceScaleFactor: 1, mobile: false });
  }
  await click(`(() => {
    const box = document.querySelector('div[aria-label="Текст предложения от ИИ"] input[type="checkbox"]');
    if (!box || box.disabled) return false;
    box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button = [...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
      .find(item => item.textContent.includes('Утвердить КП и подготовить PDF'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const section = document.querySelector('section[aria-label="Текущее коммерческое предложение"]');
    return !!section?.querySelector('section[aria-label="Утверждённое предложение"]')
      && [...section.querySelectorAll('a')].some(link => link.textContent.includes('Скачать КП PDF'));
  })()`, 'manager approves the generated offer as a saved revision');
  await until(`!!document.querySelector('div[aria-label="Сохранённые варианты КП"] .composition-card')`, 'alternative snapshot is saved with approved document');
  await until(`document.querySelector('section[aria-label="Утверждённое предложение"]').innerText.includes('Краткое предложение · #1D4ED8')`, 'selected appearance persists with immutable offer');
  const download = await evaluate(`(async () => {
    const section = document.querySelector('section[aria-label="Текущее коммерческое предложение"]');
    const link = [...section.querySelectorAll('a')].find(item => item.textContent.includes('Скачать КП PDF'));
    const response = await fetch(link.href);
    const bytes = new Uint8Array(await response.arrayBuffer());
    const valid=response.ok && String.fromCharCode(...bytes.slice(0,5)) === '%PDF-';
    const encoded=valid?await new Promise(resolve=>{
      const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.readAsDataURL(new Blob([bytes],{type:'application/pdf'}));
    }):'';
    return {valid,base64:encoded.split(',')[1],quoteUrl:link.href.split('/pdf')[0]};
  })()`);
  if (!download.valid) throw Error('Approved offer PDF is unavailable');
  const recipientPersisted = await evaluate(`(async () => {
    const response = await fetch(${JSON.stringify(download.quoteUrl)});
    const saved = await response.json();
    return response.ok && saved.approved && saved.document?.recipient_name === 'Учебный заказчик'
      && saved.document.recipient_details?.inn === '7736207543'
      && saved.document.recipient_details.short_name === 'Учебный заказчик'
      && saved.document.appearance.layout_version === 2
      && saved.document.company_profile.data.logo.width === 360;
  })()`);
  if (!recipientPersisted) throw Error('Approved document did not preserve the explicitly applied recipient registry details.');
  writeFileSync(new URL(process.env.KP_DESK_E2E_RELEASE_RUNTIME==='1'
    ? '../../artifacts/local-release-20261102/commercial-offer-compact-clone.pdf'
    : process.env.KP_DESK_E2E_FEEDBACK_STORAGE==='1'
    ? '../../artifacts/ml-feedback-storage-20261102/commercial-offer-compact-clone.pdf'
    : '../../artifacts/ml-feedback-20261102/commercial-offer-compact-clone.pdf',import.meta.url),Buffer.from(download.base64,'base64'));
  await click(`(() => {
    const button=[...document.querySelectorAll('button')].find(el=>el.textContent==='Создать КП по этому шаблону');
    if (!button || button.disabled) return false;button.click();return true;
  })()`);
  await until(`(() => {
    const rows=[...document.querySelectorAll('.tenant-quote-line')];
    return rows.length===2 && !document.querySelector('section[aria-label="Текущее коммерческое предложение"]')
      && rows.every(row=>!row.querySelector('.tenant-ml-results select')
        && row.querySelector('.tenant-extracted-review input[type="checkbox"]:not(:checked)'));
  })()`, 'saved offer creates an independent unapproved draft with fresh SKU review');
  const originalIntact=await evaluate(`(async()=>{
    const result=await fetch(${JSON.stringify(download.quoteUrl)});const quote=await result.json();
    return result.ok && quote.approved && quote.document.appearance.template==='compact';
  })()`);
  if (!originalIntact) throw Error('Using a template changed the approved original');
  console.log('AI proposal text → manager approval → immutable client PDF: PASS');
}
