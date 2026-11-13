import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** Synthetic company facts saved through the same controls used by an owner. */
export async function verifyCompanyBrowser({ command, evaluate, until, click, change }) {
  await click(`(() => {
    const link = document.querySelector('a[href="#workspace-company"]');
    if (!link) return false; link.click(); return document.querySelector('#workspace-company').open;
  })()`);
  await until(`!!document.querySelector('#workspace-company input[maxlength="200"]')`, 'owner company profile form');
  const root = await command('DOM.getDocument');
  const input = await command('DOM.querySelector', { nodeId: root.root.nodeId, selector: '#workspace-company input[type="file"]' });
  if (!input.nodeId) throw Error('Company logo upload control is missing');
  await command('DOM.setFileInputFiles', { nodeId: input.nodeId,
    files: [fileURLToPath(new URL('../../backend/tests/fixtures/company-logo.png', import.meta.url))] });
  await until(`(() => { const image = document.querySelector('#workspace-company img[alt="Логотип компании"]');
    return image?.complete && image.naturalWidth === 360 && image.naturalHeight === 100;
  })()`, 'logo is prepared and previewed before profile save');
  const unsaved = await evaluate(`(async () => {
    const org = document.querySelector('.workspace-context select').value;
    const result = await fetch('/api/session/organizations/'+encodeURIComponent(org)+'/company-profile');
    return result.ok && (await result.json()).profile === null;
  })()`);
  if (!unsaved) throw Error('Logo preparation unexpectedly saved the profile');
  await change('#workspace-company input[inputmode="numeric"]', '7736207543');
  await click(`(() => {
    const button = [...document.querySelectorAll('#workspace-company button')].find(item => item.textContent === 'Найти по ИНН');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`);
  await until(`[...document.querySelectorAll('#workspace-company button')].some(item => item.textContent === 'Применить реквизиты')`, 'mock registry lookup');
  await click(`(() => {
    const button = [...document.querySelectorAll('#workspace-company button')].find(item => item.textContent === 'Применить реквизиты');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`);
  await until(`document.querySelector('#workspace-company [aria-label="Реквизиты отправителя"]')?.innerText.includes('7736207543')`, 'structured sender details');
  await change('#workspace-company input[maxlength="200"]', 'Учебная компания');
  await change('#workspace-company textarea[maxlength="1000"]', 'Поставка офисных товаров по заявке клиента.', 'HTMLTextAreaElement');
  await change('#workspace-company textarea[maxlength="600"]', 'sales@example.invalid', 'HTMLTextAreaElement');
  await click(`(() => {
    const box = document.querySelector('#workspace-company input[type="checkbox"]');
    if (!box || box.checked) return false; box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button = [...document.querySelectorAll('#workspace-company button')]
      .find(item => item.textContent === 'Сохранить настройки компании');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`);
  await until(`document.querySelector('#workspace-company').innerText.includes('Сохранённая версия: 1')`, 'persistent company profile revision');
  const stored = await evaluate(`(async () => {
    const org = document.querySelector('.workspace-context select').value;
    const response = await fetch('/api/session/organizations/'+encodeURIComponent(org)+'/company-profile');
    const body = await response.json();
    return response.ok && body.profile.revision===1 && body.profile.data.contacts==='sales@example.invalid'
      && body.profile.data.issuer_details.inn==='7736207543'
      && body.profile.data.issuer_details.address==='Учебный адрес'
      && body.profile.data.logo.width === 360 && body.profile.data.logo.height === 100;
  })()`);
  if (!stored) throw Error('Company profile did not persist');
  if (process.env.KP_DESK_E2E_CAPTURE_COMPANY === '1') {
    for (const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await evaluate(`new Promise(resolve => requestAnimationFrame(() => {
        document.querySelector('#workspace-company').scrollIntoView({block:'start',behavior:'instant'});
        resolve(true);
      }))`);
      const overflow = await evaluate('document.documentElement.scrollWidth > innerWidth');
      if (overflow) throw Error('Company profile form overflows '+name);
      const shot = await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(new URL('../../artifacts/company-logo-20261030/company-profile-'+name+'.png',import.meta.url),
        Buffer.from(shot.data,'base64'));
    }
    await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  }
  await click(`(() => {
    document.querySelector('a[href="#workspace-catalog"]').click(); return true;
  })()`);
  console.log('Owner company settings → persisted tenant revision: PASS');
}
