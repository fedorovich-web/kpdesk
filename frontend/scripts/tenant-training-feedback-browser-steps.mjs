import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export async function verifyTrainingFeedbackBrowser({ command, evaluate, until, click }) {
  const selector = '.training-feedback';
  const storage=process.env.KP_DESK_E2E_FEEDBACK_STORAGE==='1';
  const output = new URL(process.env.KP_DESK_E2E_RELEASE_RUNTIME==='1' ? '../../artifacts/local-release-20261102/'
    : storage ? '../../artifacts/ml-feedback-storage-20261102/' : '../../artifacts/ml-feedback-20261102/', import.meta.url);
  await command('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: fileURLToPath(output) });
  await until(`!!document.querySelector(${JSON.stringify(selector)})`, 'owner training feedback is available');
  await click(`(() => { const section=document.querySelector(${JSON.stringify(selector)});
    if (!section.open) section.querySelector('summary').click(); return section.open; })()`);
  await click(`(() => { const button=[...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
    .find(item=>item.textContent==='Загрузить товары для оценки');
    if (!button || button.disabled) return false; button.click(); return true; })()`);
  await until(`document.querySelectorAll(${JSON.stringify(selector + ' fieldset')}).length>0`, 'real candidates loaded');
  await evaluate(`(() => { const fields=[...document.querySelectorAll(${JSON.stringify(selector + ' fieldset')})];
    if (fields.some(field=>field.querySelector('select').value || field.querySelector('input:checked')))
      throw Error('Training decisions must not be inferred from quote selection');
    window.__kpdeskFeedbackBlob=null;
    const original=URL.createObjectURL;
    URL.createObjectURL=function(blob) { if (blob.type==='application/json') blob.text().then(text=>{
      window.__kpdeskFeedbackBlob=JSON.parse(text); }); return original.call(URL,blob); };
    for (const field of fields) { const select=field.querySelector('select');
      if (!field.dataset.selectedSku) throw Error('Synthetic quote must have selected SKU');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,field.dataset.selectedSku);
      select.dispatchEvent(new Event('change',{bubbles:true})); }
    return true; })()`);
  await until(`(() => { const fields=[...document.querySelectorAll(${JSON.stringify(selector + ' fieldset')})];
    return fields.every(field=>field.querySelector('select').value===field.dataset.selectedSku
      && ![...field.querySelectorAll('label')].some(label=>label.textContent.includes('('+field.dataset.selectedSku+')')
        && label.querySelector('input'))); })()`, 'explicit selected labels applied');
  await evaluate(`(() => { for (const field of document.querySelectorAll(${JSON.stringify(selector + ' fieldset')})) {
    const rejected=field.querySelector('input[type="checkbox"]');
    if (!rejected) throw Error('Synthetic catalog must contain a negative candidate'); rejected.click(); }
    return true; })()`);
  await click(`(() => { const section=document.querySelector(${JSON.stringify(selector)});
    const label=[...section.querySelectorAll('label')].find(item=>item.textContent.includes('Я проверил оценки'));
    const checkbox=label?.querySelector('input'); if (!checkbox || checkbox.disabled) return false;
    if (!checkbox.checked) checkbox.click(); return checkbox.checked; })()`);
  await click(`(() => { const button=[...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
    .find(item=>item.textContent==='Скачать разметку для обучения');
    if (!button || button.disabled) return false; button.click(); return true; })()`);
  await until(`!!window.__kpdeskFeedbackBlob && document.querySelector(${JSON.stringify(selector)})
    .innerText.includes('Файл разметки скачан')`, 'server-authorized JSON downloaded through React');
  if(storage){
    await click(`(() => { const section=document.querySelector(${JSON.stringify(selector)});
      const label=[...section.querySelectorAll('label')].find(item=>item.textContent.includes('Я проверил оценки'));
      const checkbox=label?.querySelector('input'); if(!checkbox || checkbox.checked) return false;
      checkbox.click(); return true; })()`);
    await click(`(() => {const button=[...document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button')]
      .find(item=>item.textContent==='Сохранить оценки в организации');
      if(!button || button.disabled) return false;button.click();return true;})()`);
    await until(`document.querySelector(${JSON.stringify(selector)}).innerText.includes('Оценки сохранены в организации')`, 'durable labels saved');
    await click(`(() => { const details=document.querySelector('.training-feedback-history');
      if(!details.open) details.querySelector('summary').click();return details.open;})()`);
    await click(`(() => {const button=[...document.querySelector('.training-feedback-history').querySelectorAll('button')]
      .find(item=>item.textContent==='Загрузить историю оценок');if(!button || button.disabled) return false;
      button.click();return true;})()`);
    await until(`document.querySelector('.training-feedback-history').innerText.includes('Ожидает подготовки набора')`, 'saved history reopened through API');
    await click(`(() => {const button=[...document.querySelector('.training-feedback-history').querySelectorAll('button')]
      .find(item=>item.textContent==='Скачать сохранённую разметку');if(!button || button.disabled) return false;
      window.__kpdeskFeedbackBlob=null;button.click();return true;})()`);
    await until('!!window.__kpdeskFeedbackBlob', 'persisted bundle downloaded');
    await click(`(() => {const button=[...document.querySelector('.training-feedback-history').querySelectorAll('button')]
      .find(item=>item.textContent==='Отозвать согласие на обучение');if(!button || button.disabled) return false;
      button.click();return true;})()`);
    await until(`document.querySelector('.training-feedback-history').innerText.includes('Согласие отозвано')
      && ![...document.querySelector('.training-feedback-history').querySelectorAll('button')]
        .some(item=>item.textContent==='Скачать сохранённую разметку')`, 'withdrawal hides persisted export action');
    writeFileSync(new URL('browser-storage-checks.json',output),JSON.stringify({
      real_react_auth_postgres:true,save_history_download_withdraw:true,model_activated:false,
    },null,2));
  }
  const bundle = await evaluate('window.__kpdeskFeedbackBlob');
  if (bundle.format !== 'kpdesk-manager-feedback-v1' || bundle.training_data_kind !== 'manager_reviewed'
    || !bundle.events.length || bundle.catalog.some(product => Number(product.price_rub) !== 0)) {
    throw Error('Invalid private training export');
  }
  writeFileSync(new URL('browser-feedback-bundle.json', output), JSON.stringify(bundle, null, 2));
  const viewport = await evaluate('({width:innerWidth,height:innerHeight})');
  const originalAnchor = await evaluate(`(() => {
    const value=document.documentElement.style.overflowAnchor;
    document.documentElement.style.overflowAnchor='none'; return value;
  })()`);
  const captures = [];
  try {
    for (const [name,width,height,bottom] of [
      ['desktop',1440,1000,false],['mobile',390,900,false],['mobile-bottom',390,900,true],
    ]) {
      await command('Emulation.setDeviceMetricsOverride', { width,height,deviceScaleFactor:1,mobile:false });
      await until(`innerWidth===${width}`, 'feedback screenshot viewport settled');
      await evaluate(`(() => {
        const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
        scrollTo({left:0,top:scrollY+${bottom ? 'r.bottom-innerHeight+16' : 'r.top-16'},behavior:'instant'});
      })()`);
      const rect = await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>{
        const section=document.querySelector(${JSON.stringify(selector)}); const r=section.getBoundingClientRect();
        if (section.scrollWidth>section.clientWidth+1 || document.documentElement.scrollWidth>innerWidth+1)
          throw Error('Training feedback horizontal overflow');
        if (${bottom ? 'r.bottom>innerHeight || r.bottom<innerHeight-32' : 'r.top<0 || r.top>32'})
          throw Error('Training feedback screenshot edge outside viewport');
        resolve({x:r.left,y:r.top,width:r.width,height:r.height,bottom:r.bottom});
      })))`);
      // A viewport screenshot does not resize the capture surface around a long
      // page-coordinate clip. Mobile uses two overlapping, unmodified viewports.
      const shot = await command('Page.captureScreenshot', { format:'png',captureBeyondViewport:false });
      writeFileSync(new URL('feedback-'+name+'.png', output),Buffer.from(shot.data,'base64'));
      captures.push({name,viewport_width:width,viewport_height:height,section_rect:rect});
    }
  } finally {
    await evaluate(`document.documentElement.style.overflowAnchor=${JSON.stringify(originalAnchor)}`);
    await command('Emulation.setDeviceMetricsOverride', { ...viewport,deviceScaleFactor:1,mobile:false });
  }
  writeFileSync(new URL('browser-feedback-checks.json', output),JSON.stringify({
    real_react_auth_https_postgres:true, inferred_decisions:false, model_activated:false,
    explicit_events:bundle.events.length, prices_exported:false, widths_checked:[1440,390],captures,
  },null,2));
}
