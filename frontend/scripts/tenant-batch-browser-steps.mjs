/** A71: run the actual React batch reviewer on an isolated HTTPS browser. */
export async function verifySupplierBatchBrowser({ evaluate, until, click, change, org }) {
  const source = [
    'sku;name;price;currency;category;unit;pack',
    'KB-A;Office keyboard wired 104 keys;1990.00;RUB;keyboards;pcs;1',
    'KB-B;Office keyboard wired 104 keys second;2100.00;RUB;keyboards;pcs;1',
    'Section: peripherals',
  ].join('\n') + '\n';
  await until('!!document.querySelector(".supplier-precision form input[type=file]")',
    'A71 supplier review form');
  await change('.supplier-precision form input[maxlength="120"]', 'vendor-a71');
  const attached = await evaluate(`(() => {
    const input = document.querySelector('.supplier-precision form input[type=file]');
    if (!input) return false;
    const transfer = new DataTransfer();
    transfer.items.add(new File([${JSON.stringify(source)}], 'a71-vendor.csv',
      {type:'text/csv'}));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change',{bubbles:true}));
    return true;
  })()`);
  if (!attached) throw Error('A71 original supplier source input unavailable');
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-precision form button')]
      .find(item=>item.textContent.includes('Определить столбцы'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until('!!document.querySelector(".supplier-inspection")',
    'A71 supplier inspected');
  const inspected = await evaluate(`(() => {
    const section=document.querySelector('.supplier-inspection');
    return section?.innerText.includes('Обнаружено строк:')
      && section?.innerText.includes('currency')
      && section?.innerText.includes('category');
  })()`);
  if (!inspected) throw Error('A71 source mapping evidence not shown');
  for (const [label, value] of [
    ['Столбец SKU','1'], ['Столбец названия','2'],
    ['Столбец цены','3'], ['Столбец валюты','4'],
    ['Столбец категории','5'], ['Столбец единицы продажи','6'],
    ['Столбец количества в упаковке','7'],
  ]) {
    const changed = await evaluate(`(() => {
      const label=[...document.querySelectorAll('.supplier-precision .supplier-columns label')]
        .find(node=>node.textContent.trim()===${JSON.stringify(label)});
      const input=label?.querySelector('input[type=number]');
      if (!input) return false;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(
        input,${JSON.stringify(value)});
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
      return true;
    })()`);
    if (!changed) throw Error('A71 mapping field missing: ' + label);
  }
  await click(`(() => {
    const label=[...document.querySelectorAll('.supplier-precision label')]
      .find(node=>node.textContent.includes('Я проверил соответствие всех столбцов оригиналу'));
    const input=label?.querySelector('input[type=checkbox]');
    if (!input || input.checked) return false;
    input.click(); return input.checked;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-precision button')]
      .find(item=>item.textContent.includes('Просмотреть все строки прайса'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(() => {
    const editor=document.querySelector('.supplier-decision');
    return !!editor && editor.textContent.includes('Проверено 0 из 3')
      && !![...editor.querySelectorAll('button')]
        .find(button=>button.textContent.includes('Строка 4:'));
  })()`, 'A71 canonical editor loaded');
  const before = await evaluate(`(async () => {
    const response=await fetch('/api/session/organizations/' +
      ${JSON.stringify(org)} + '/catalog/current');
    if (!response.ok) throw Error('A71 missing prior catalog');
    return response.json();
  })()`);
  if (before.rows_count < 1 || !before.version_id) {
    throw Error('A71 expected a pre-existing catalog to verify no implicit publication');
  }

  for (const line of [2,3]) {
    await click(`(() => {
      const label=[...document.querySelectorAll('.supplier-decision label')]
        .find(item=>item.textContent.includes(
          'Проверил строку ' + ${line} + ' для пакетного решения'));
      const box=label?.querySelector('input[type=checkbox]');
      if (!box || box.disabled || box.checked) return false;
      box.click(); return box.checked;
    })()`);
  }
  await until(`document.querySelector('[aria-label="Пакетная проверка товаров"]')
    ?.textContent.includes('Проверенные строки текущей страницы: 2')`,
    'A71 two manually selected rows');
  await change(
    '.supplier-decision [aria-label="Пакетная проверка товаров"] select',
    'keyboard', 'HTMLSelectElement',
  );
  const setShared = async (label, value) => {
    const changed = await evaluate(`(() => {
      const label=[...document.querySelectorAll(
        '.supplier-decision [aria-label="Пакетная проверка товаров"] label'
      )].find(item=>item.textContent.includes(${JSON.stringify(label)}));
      const input=label?.querySelector('input');
      if (!input) return false;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(
        input,${JSON.stringify(value)});
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
      return true;
    })()`);
    if (!changed) throw Error('A71 missing shared parameter: ' + label);
  };
  await setShared('Общий проверенный параметр: Подключение','wired');
  await setShared('Общий проверенный параметр: Клавиш','104');
  const preliminary = await evaluate(`(() => {
    const editor=document.querySelector('.supplier-decision');
    const publish=[...editor.querySelectorAll('button')]
      .find(b=>b.textContent.includes('Подготовить проверенный CSV'));
    const batch=[...editor.querySelectorAll('button')]
      .find(b=>b.textContent.includes('Применить проверенные параметры'));
    return {publishDisabled:publish?.disabled,batchDisabled:batch?.disabled};
  })()`);
  if (!preliminary.publishDisabled || !preliminary.batchDisabled) {
    throw Error('A71 batch or publication bypassed human confirmation');
  }
  await click(`(() => {
    const label=[...document.querySelectorAll('.supplier-decision label')]
      .find(item=>item.textContent.includes('Я сверил каждую отмеченную строку оригинала'));
    const box=label?.querySelector('input[type=checkbox]');
    if (!box || box.checked || box.disabled) return false;
    box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-decision button')]
      .find(item=>item.textContent.includes('Применить проверенные параметры'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until('document.querySelector(".supplier-decision")?.textContent.includes("Проверено 2 из 3")',
    'A71 two independent source-bound approvals');
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-decision button')]
      .find(item=>item.textContent.includes('Строка 4:'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await click(`(() => {
    const label=[...document.querySelectorAll('.supplier-decision label')]
      .find(item=>item.textContent.includes('Исключить эту строку из каталога'));
    const box=label?.querySelector('input[type=checkbox]');
    if (!box || box.checked) return false;
    box.click(); return box.checked;
  })()`);
  const changed = await evaluate(`(() => {
    const label=[...document.querySelectorAll('.supplier-decision label')]
      .find(item=>item.textContent.includes('Причина исключения'));
    const input=label?.querySelector('textarea');
    if (!input) return false;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(
      input,'Supplier section heading, not a saleable product.');
    input.dispatchEvent(new Event('input',{bubbles:true}));
    return true;
  })()`);
  if (!changed) throw Error('A71 exclusion reason field missing');
  await click(`(() => {
    const label=[...document.querySelectorAll('.supplier-decision label')]
      .find(item=>item.textContent.includes('Я лично проверил эту строку оригинала'));
    const box=label?.querySelector('input[type=checkbox]');
    if (!box || box.checked) return false;
    box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-decision button')]
      .find(item=>item.textContent.includes('Сохранить решение по строке'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until('document.querySelector(".supplier-decision")?.textContent.includes("Проверено 3 из 3")',
    'A71 all source rows reviewed');
  await click(`(() => {
    const label=[...document.querySelectorAll('.supplier-decision label')]
      .find(item=>item.textContent.includes('Я проверил тип цены:'));
    const box=label?.querySelector('input[type=checkbox]');
    if (!box || box.checked) return false;
    box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('.supplier-decision button')]
      .find(item=>item.textContent.includes('Подготовить проверенный CSV'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`document.body.innerText.includes(
    'Нормализованный CSV готов. Выполните проверку перед публикацией')`,
    'A71 canonical export accepted by backend', 20000);
  const unchanged = await evaluate(`(async () => {
    const data=await (await fetch('/api/session/organizations/' +
      ${JSON.stringify(org)} + '/catalog/current')).json();
    return data.version_id === ${JSON.stringify(before.version_id)};
  })()`);
  if (!unchanged) throw Error('A71 canonical export silently published catalog');
  await click(`(() => {
    const button=[...document.querySelectorAll('button')]
      .find(item=>item.textContent.includes('Проверить прайс перед публикацией'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`document.querySelector('[aria-label="Результат проверки прайса"]')
    ?.innerText.includes('готов к публикации')`, 'A71 reviewed CSV preflight');
  const status = await evaluate(`(() => {
    const region=document.querySelector('[aria-label="Результат проверки прайса"]');
    const button=[...document.querySelectorAll('button')]
      .find(b=>b.textContent.includes('Опубликовать прайс'));
    return {text:region?.innerText, blocked:button?.disabled};
  })()`);
  if (!status.blocked || !status.text.includes('Товаров: 2')) {
    throw Error('A71 preflight bypass or wrong product count: ' + JSON.stringify(status));
  }
  await click(`(() => {
    const label=[...document.querySelectorAll('label')]
      .find(item=>item.textContent.includes('Я проверил файл и подтверждаю публикацию'));
    const box=label?.querySelector('input[type=checkbox]');
    if (!box || box.checked || box.disabled) return false;
    box.click(); return box.checked;
  })()`);
  await click(`(() => {
    const button=[...document.querySelectorAll('button')]
      .find(item=>item.textContent.includes('Опубликовать прайс'));
    if (!button || button.disabled) return false;
    button.click(); return true;
  })()`);
  await until(`(async () => {
    const response=await fetch('/api/session/organizations/' +
      ${JSON.stringify(org)} + '/catalog/current');
    if (!response.ok) return false;
    const current=await response.json();
    return current.rows_count === 2
      && current.version_id !== ${JSON.stringify(before.version_id)};
  })()`, 'A71 reviewed catalog published as a NEW version');
  const final = await evaluate(`(async () => {
    const response=await fetch('/api/session/organizations/' +
      ${JSON.stringify(org)} + '/catalog/current');
    if (!response.ok) throw Error('A71 published catalog missing');
    return response.json();
  })()`);
  if (final.rows_count !== 2 || final.version_id === before.version_id) {
    throw Error('A71 reviewed publication failed version check');
  }
  console.log('A71 HTTPS React batch → 2 source-bound approvals + exclusion →'
    + ' export SHA → preflight → explicit publish: PASS');
}
