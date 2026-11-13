import { writeFileSync } from 'node:fs';

/** Authenticated synthetic fixture; inspect the real layout at both breakpoints. */
export async function verifyWorkspaceLayout({ command, evaluate }) {
  const results = [];
  for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
    await command('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    await evaluate(`new Promise(resolve => requestAnimationFrame(() => {
      scrollTo({top:0,left:0,behavior:'instant'}); resolve(true);
    }))`);
    const layout = await evaluate(`(() => {
      const rect = selector => {
        const box = document.querySelector(selector).getBoundingClientRect();
        return {left:box.left,right:box.right,width:box.width};
      };
      return {header:rect('.access-header'),main:rect('.access-main-workspace'),
        logo:rect('.access-header img'),viewport:innerWidth,pageWidth:document.documentElement.scrollWidth,
        teamClosed:!document.querySelector('#workspace-team').open,
        toolsClosed:!document.querySelector('#workspace-sources').open,
        order:['workspace-catalog','workspace-request','workspace-quote-editor','workspace-quotes','workspace-team','workspace-sources']
          .map(id => document.getElementById(id).getBoundingClientRect().top)};
    })()`);
    if (Math.abs(layout.header.left-layout.main.left)>1 || Math.abs(layout.header.right-layout.main.right)>1
      || Math.abs(layout.logo.left-layout.main.left)>1) throw Error('Workspace/logo alignment failed: '+JSON.stringify(layout));
    if (layout.pageWidth>width || !layout.teamClosed || !layout.toolsClosed
      || layout.order.some((top,index) => index>0 && top<layout.order[index-1])) {
      throw Error('Workspace layout/order/disclosure failed: '+JSON.stringify(layout));
    }
    const screenshot = await command('Page.captureScreenshot', {format:'png',captureBeyondViewport:false});
    writeFileSync(new URL('../../artifacts/engineering-audit-20261028/workspace-'+name+'.png',import.meta.url),
      Buffer.from(screenshot.data,'base64'));
    results.push({name,...layout});
  }
  writeFileSync(new URL('../../artifacts/engineering-audit-20261028/workspace-layout.json',import.meta.url),JSON.stringify(results,null,2));
  await command('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
  console.log('Workspace desktop/mobile alignment, disclosure and task order: PASS');
}
