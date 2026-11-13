/** Isolated A91 Chrome CDP: genuine React controls → HTTPS → PostgreSQL.
 * No mocked fetch, no direct SQL writes by this browser, no API shortcuts.
 */
export async function verifySupplierLargeBrowser({ command, evaluate, until, click, url, org }) {
  await command('Page.navigate', { url: url + '/__e2e/e2e/large-review/' });
  await until('document.querySelector("h1")?.textContent.includes("A91 isolated React")',
    'A91 independent React entry', 16000);
  const saved = count => `document.body.innerText.includes('На сервере сохранено ${count} из 121 решений.')`;
  const publication = `[...document.querySelectorAll('button')].find(
    item => item.textContent.includes('Проверить и опубликовать весь прайс'))`;
  const save = `(() => {
    const button=[...document.querySelectorAll('button')].find(
      item => item.textContent.includes('Сохранить строки текущей страницы:'));
    if (!button || button.disabled) return false; button.click(); return true;
  })()`;
  const next = `(() => {
    const button=[...document.querySelectorAll('button')].find(
      item => item.textContent.trim() === 'Next reviewed source page');
    if (!button || button.disabled) return false; button.click(); return true;
  })()`;
  const initiallyBlocked = await evaluate(`(() => {
    const button=${publication}; return button?.disabled === true;
  })()`);
  if (!initiallyBlocked) throw Error('A91 cannot publish an unsaved source.');
  for (const [n, count] of [[1,45],[2,90],[3,121]]) {
    await until(`[...document.querySelectorAll('button')].some(
      item => item.textContent.includes('Сохранить строки текущей страницы:')
        && !item.disabled)`, 'A91 page ' + n + ' save unlocked');
    await click(save);
    await until(saved(count), 'A91 committed source review page ' + n, 20000);
    if (count !== 121) {
      const isBlocked = await evaluate(`(() => {
        const button=${publication}; return button?.disabled === true;
      })()`);
      if (!isBlocked) throw Error('A91 premature catalog publication');
      await click(next);
      await until(`document.querySelector('[data-testid="page"]')?.textContent
        .includes('Batch ${n + 1}')`, 'A91 next source page');
    }
  }
  await click(`(() => {
    const b=[...document.querySelectorAll('button')].find(
      item=>item.textContent.trim()==='Remount review component');
    if(!b)return false; b.click(); return true;
  })()`);
  // Deliberately remount: the in-memory saved checkpoint is gone. Restore
  // all 121 confirmed decisions from genuine paged HTTPS GET/POST calls.
  await until(saved(0), 'A91 local component state discarded');
  await click(`(() => {
    const input=[...document.querySelectorAll('input[type="checkbox"]')]
      .find(item=>item.parentElement?.textContent.includes(
        'Я понимаю, что восстановление заменит несохранённые локальные решения'));
    if(!input || input.checked)return false; input.click(); return true;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('button')].find(
      item=>item.textContent.includes('Восстановить прогресс проверки с сервера'));
    if(!button || button.disabled)return false; button.click(); return true;
  })()`);
  await until(`document.querySelector('[data-testid="restored"]')
    ?.textContent.includes('121') && ${saved(121)}`,
  'A91 recovered complete 121 row snapshot', 20000);
  const ready = await evaluate(`(() => {
    const button=${publication}; return button?.disabled === false;
  })()`);
  if (!ready) throw Error('A91 recovered snapshot did not unlock publication.');
  await click(`(() => {
    const button=${publication}; if(!button||button.disabled)return false;
    button.click(); return true;
  })()`);
  await until('!!document.querySelector("[data-testid=published]")',
    'A91 published actual catalog', 30000);
  const outcome = await evaluate(`(async () => {
    const proof=JSON.parse(document.querySelector('[data-testid=published]').textContent);
    const response=await fetch('/api/session/organizations/' +
      ${JSON.stringify(org)} + '/catalog/current', {cache:'no-store'});
    if (!response.ok) throw Error('A91 current catalog HTTP query failed');
    const actual=await response.json();
    return {proof,actual};
  })()`);
  if (!outcome?.proof?.version_id
    || outcome.proof.version_id !== outcome.actual.version_id
    || outcome.proof.rows_count !== 120 || outcome.actual.rows_count !== 120
    || !/^[0-9a-f]{64}$/.test(outcome.proof.source_sha256)) {
    throw Error('A91 React/publication/active pointer diverged: ' + JSON.stringify(outcome));
  }
  console.log('A91 React → real HTTPS 3 review pages → restore 121 → signed PostgreSQL publish 120: PASS');
}
