import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, X } from 'lucide-react';
import type { Page, pagePaths } from '../navigation';

export const demoTourSteps: { page: keyof typeof pagePaths; target: string; title: string; text: string }[] = [
  { page: 'import', target: 'activate', title: 'Начните с прайса', text: 'Мы открыли прайс с пятью учебными товарами. Нажимайте «Далее», чтобы пройти путь до PDF. После закрытия подсказок вы вернётесь к своей работе в демо.' },
  { page: 'prices', target: 'catalog', title: 'Проверьте загруженные товары', text: 'В прайсе видны названия, артикулы и цены. По этим данным KPDesk предлагает товары для заявки и рассчитывает стоимость предложения.' },
  { page: 'prices', target: 'catalog-search', title: 'Найдите нужный товар', text: 'Поиск работает по названию, артикулу и бренду. Фильтр оставляет товары выбранной категории. Можно попробовать поиск или сразу перейти дальше.' },
  { page: 'request', target: 'source', title: 'Добавьте заявку клиента', text: 'Сюда можно вставить текст письма, заказа или спецификации. В примере уже есть заявка на бумагу и ручки.' },
  { page: 'request', target: 'extract', title: 'Откройте позиции заявки', text: 'Кнопка показывает результат разбора: товары, количество и характеристики. В этом примере результат подготовлен заранее. Нажмите «Далее», чтобы его увидеть.' },
  { page: 'request', target: 'products', title: 'Сверьте позиции с заявкой', text: 'В заявке три позиции. Проверьте названия и характеристики, при необходимости исправьте их. Недостающие данные можно уточнить у клиента и добавить вручную.' },
  { page: 'request', target: 'quantity', title: 'Проверьте количество и единицы', text: 'Количество можно исправить в каждой строке. Обратите внимание на единицу: две пачки бумаги и два листа — разный объём заказа.' },
  { page: 'request', target: 'rules', title: 'Задайте условия расчёта', text: 'Здесь можно указать наценку, бюджет и ограничения по брендам. Условия нужно проверить и подтвердить. В подсказках продолжим с исходными ценами прайса.' },
  { page: 'request', target: 'calculate', title: 'Подберите товары и рассчитайте КП', text: 'Кнопка запускает подбор по прайсу и расчёт стоимости. Нажмите «Далее»: мы покажем результат для подготовленной заявки.' },
  { page: 'review', target: 'results', title: 'Проверьте результат подбора', text: 'У каждой позиции видны артикул, результат подбора и сумма. Сверьте выбранный товар с требованиями клиента, прежде чем переходить к документу.' },
  { page: 'review', target: 'exceptions', title: 'Уточните позицию без товара', text: 'В прайсе нет бумаги 120 г/м², поэтому эта строка осталась без товара. Бумага 80 г/м² её не заменяет. На следующем шаге покажем пример без третьей позиции. В рабочем КП изменение состава нужно согласовать с клиентом.' },
  { page: 'review', target: 'total', title: 'Проверьте итоговую сумму', text: 'После удаления третьей позиции остались бумага и ручки. Для них подобраны товары и рассчитана общая стоимость. Теперь можно открыть документ.' },
  { page: 'preview', target: 'preview', title: 'Посмотрите готовый документ', text: 'Проверьте текст, таблицу товаров, цены и итоговую сумму. Учебный документ помечен «ДЕМО».' },
  { page: 'preview', target: 'templates', title: 'Выберите оформление', text: 'Переключите шаблон и посмотрите, как меняется оформление. Товары, текст и расчёт сохранятся.' },
  { page: 'preview', target: 'print', title: 'Сохраните PDF', text: 'Кнопка откроет печать браузера. Выберите сохранение в PDF, чтобы скачать учебный документ. Для КП со своим прайсом и данными компании перейдите в личный кабинет.' },
];

/** Nonmodal product tour: Next navigates prepared demo stages. */
export function DemoTour({ page, busy, onStepChange, onClose }: {
  page: Page; busy: boolean; onStepChange: (index: number) => void; onClose: () => void;
}) {
  const [index, setIndex] = useState(0);
  const panel = useRef<HTMLElement>(null);
  const step = demoTourSteps[index]!;
  const onStepPage = page === step.page;
  useEffect(() => { panel.current?.focus({ preventScroll: true }); }, [index]);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose]);
  useEffect(() => {
    if (!onStepPage) return;
    const element = document.querySelector<HTMLElement>(`.demo-tour-running [data-demo-target="${step.target}"]`);
    if (!element) return;
    element.dataset.demoSpotlight = 'true';
    const frame = requestAnimationFrame(() => {
      const bounds = element.getBoundingClientRect();
      if (bounds.height === 0) return;
      const offset = (panel.current?.getBoundingClientRect().height ?? 0) + 32;
      window.scrollTo({ top: Math.max(0, window.scrollY + bounds.top - offset), behavior: 'instant' });
    });
    return () => { cancelAnimationFrame(frame); delete element.dataset.demoSpotlight; };
  }, [step.target, onStepPage, page, index]);
  const move = (next: number) => { onStepChange(next); setIndex(next); };
  return <section ref={panel} tabIndex={-1} className="panel demo-tour" aria-label="Пошаговое знакомство с демо">
    <div className="demo-tour-heading">
      <div><p className="demo-tour-progress" role="status">Шаг {index + 1} из {demoTourSteps.length}</p><h2>{step.title}</h2></div>
      <button className="button button-white demo-tour-close" type="button" onClick={onClose} aria-label="Закрыть подсказки"><X size={18} aria-hidden="true"/></button>
    </div>
    <p className="demo-tour-description">{step.text}</p>
    <div className="demo-tour-controls">
      <button className="button button-white" type="button" disabled={index === 0 || busy} onClick={() => move(index - 1)}><ArrowLeft size={16} aria-hidden="true"/>Назад</button>
      <a className="text-button demo-tour-exit" href="/">Выйти из демо</a>
      {!onStepPage && <button className="text-button" type="button" disabled={busy} onClick={() => onStepChange(index)}>Открыть этот шаг</button>}
      <button className="button button-primary" type="button" disabled={busy} onClick={() => index === demoTourSteps.length - 1 ? onClose() : move(index + 1)}>
        {index === demoTourSteps.length - 1 ? 'Завершить подсказки' : 'Далее'}<ArrowRight size={16} aria-hidden="true"/>
      </button>
    </div>
  </section>;
}
