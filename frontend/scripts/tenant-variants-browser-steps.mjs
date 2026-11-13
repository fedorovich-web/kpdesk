import { writeFileSync } from 'node:fs';

/** Compare through authenticated API and deliberately transfer the reviewed SKU set. */
export async function verifyVariantsBrowser({ command, evaluate, until, click, change }) {
  const section = 'section[aria-label="Варианты комплектации"]';
  const previous = await evaluate(`[...document.querySelectorAll('.tenant-ml-results select')].map(el => el.value)`);
  await click(`(() => {
    const button = [...document.querySelector(${JSON.stringify(section)}).querySelectorAll('button')]
      .find(el => el.textContent === 'Сравнить варианты комплектации');
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`!!document.querySelector(${JSON.stringify(section + ' .composition-card')})`, 'real catalog composition comparison');
  const valid = await evaluate(`(() => {
    const card = document.querySelector(${JSON.stringify(section + ' .composition-card')});
    return card.querySelectorAll('li').length === 2 && card.querySelector('button').disabled
      && card.innerText.includes('Итого:') && card.innerText.includes('E2E-PAPER-A');
  })()`);
  if (!valid) throw Error('Variant composition, money or manual review gate failed');
  for (const [name, width, height] of [['desktop',1440,1000],['mobile',390,844]]) {
    await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
    await evaluate(`new Promise(resolve => requestAnimationFrame(() => {
      const target=document.querySelector(${JSON.stringify(section)});
      scrollTo({top:scrollY+target.getBoundingClientRect().top,left:0,behavior:'instant'});
      requestAnimationFrame(()=>resolve(true));
    }))`);
    const layout = await evaluate(`(() => ({width:innerWidth,page:document.documentElement.scrollWidth,
      top:document.querySelector(${JSON.stringify(section)}).getBoundingClientRect().top,
      cards:[...document.querySelectorAll('.composition-card')].map(el => ({width:el.getBoundingClientRect().width,
        scroll:el.scrollWidth,client:el.clientWidth}))}))()`);
    if (Math.abs(layout.top)>2 || layout.page>width || layout.cards.some(card=>card.scroll>card.client+1)) throw Error('Variant layout: '+JSON.stringify(layout));
    if (process.env.KP_DESK_E2E_CAPTURE_VARIANTS === '1') {
      const shot = await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(new URL('../../artifacts/engineering-audit-20261028/variants-'+name+'.png',import.meta.url),Buffer.from(shot.data,'base64'));
    }
  }
  await click(`(() => {
    document.querySelector(${JSON.stringify(section + ' .composition-card input[type="checkbox"]')}).click();
    document.querySelector(${JSON.stringify(section + ' .composition-card button')}).click(); return true;
  })()`);
  await until(`[...document.querySelectorAll('.tenant-ml-results select')].every(el=>el.value==='E2E-PAPER-A')`, 'reviewed variant populates actual SKU controls');
  // Customizing a variant is supported. Keep the pre-existing A59 persistence
  // contract (A/B) so its independent immutable-money assertions remain useful.
  for (let index=0;index<previous.length;index++) {
    await change('.tenant-quote-line:nth-of-type('+(index+1)+') .tenant-ml-results select',previous[index],'HTMLSelectElement');
  }
  console.log('Catalog variants → explicit review → editor SKU selections, desktop/mobile: PASS');
}
