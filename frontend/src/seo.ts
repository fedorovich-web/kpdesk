/** Route-specific search copy based on the implemented request-to-approved-PDF workflow. */
import { launchPlans, rubles } from './content/pricing';
export const publicSeo = {
  title: 'ИИ-генератор коммерческих предложений из прайса — KPDesk',
  description: 'Загрузите прайс и заявку. ИИ выделит требования и подготовит текст КП. Выберите товары из своего каталога, проверьте сумму и скачайте PDF с логотипом.',
  h1: 'ИИ-генератор КП по заявке и прайсу',
  lead: 'Вставьте заявку клиента и загрузите прайс. ИИ разберёт требования и подготовит текст, а KPDesk подберёт товары и рассчитает сумму по вашему выбору. Проверьте КП и скачайте PDF с реквизитами и логотипом.',
} as const;

export const publicSections = [{
  id: 'features',
  heading: 'Разбор заявки, подбор товаров и расчёт КП',
  items: [
    { heading: 'Разбор заявки с ИИ', body: 'Получите список позиций, количества и характеристик из письма. Уточните требования, если в заявке не хватает данных.' },
    { heading: 'Подбор товаров из вашего прайса', body: 'Сравните предложенные позиции с заявкой и подтвердите товар для каждой строки.' },
    { heading: 'Расчёт по ценам из прайса', body: 'После выбора KPDesk посчитает стоимость каждой строки и итог. Сумма обновится, если вы измените товары или количество.' },
    { heading: 'Генерация текста КП', body: 'Задайте условия продажи. ИИ подготовит текст по выбранным товарам, а вы проверите его и утвердите документ.' },
  ],
}] as const;

export const metadata = {
  public: publicSeo,
  faq: {
    title: 'Справка KPDesk — прайсы, ИИ, PDF и оплата',
    description: 'Как загрузить прайс, проверить предложенные товары, оформить КП в PDF и оплатить тариф. Ответы об ИИ, доступе команды и работе с данными.',
  },
  features: {
    title: 'ИИ для создания КП, подбор товаров и расчёт — KPDesk',
    description: 'Сверьте товары с требованиями клиента, сравните варианты и проверьте расчёт. Оформите КП с реквизитами, логотипом и текстом, подготовленным ИИ.',
  },
  how: {
    title: 'Как собрать КП из заявки и прайса — 5 шагов в KPDesk',
    description: 'Что подготовить для первого КП и в каком порядке работать. Загрузите прайс, проверьте требования и товары, оформите и скачайте утверждённый PDF.',
  },
  integrations: {
    title: 'Импорт прайса Excel и CSV для расчёта КП — KPDesk',
    description: 'Какие столбцы нужны для подбора товаров и расчёта КП. Загрузите CSV, XLSX или приведите к этому формату выгрузку из 1С, CRM или Google Sheets.',
  },
  pricing: {
    title: `Тарифы KPDesk — доступ от ${rubles(launchPlans[0].priceRub)} за 30 дней`,
    description: `${launchPlans.map(plan => `${plan.name} ${rubles(plan.priceRub)}`).join(', ')} за 30 дней для организации. Сравните лимиты сотрудников, товаров и новых КП.`,
  },
  demo: {
    title: 'Демо генератора КП без регистрации — KPDesk',
    description: 'Пройдите пошаговое демо KPDesk с учебной заявкой и прайсом. Выберите товары и посмотрите, как меняется сумма. Демо не вызывает внешний ИИ.',
  },
  privacy: {
    title: 'Политика конфиденциальности — KPDesk (проект)',
    description: 'Проект документа о порядке обработки персональных данных KPDesk.',
  },
} as const;

export const faqSeo = metadata.faq;
export type SeoRoute = keyof typeof metadata | 'app' | 'notFound';

const routePaths: Record<keyof typeof metadata, string> = {
  public: '/', faq: '/faq', features: '/features', how: '/how-it-works',
  integrations: '/integrations', pricing: '/pricing', demo: '/demo', privacy: '/privacy',
};

export function applyDocumentSeo(route: SeoRoute, title?: string): void {
  const description = document.querySelector<HTMLMetaElement>('meta[name="description"]');
  const robots = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
  const ogTitle = document.querySelector<HTMLMetaElement>('meta[property="og:title"]');
  const ogDescription = document.querySelector<HTMLMetaElement>('meta[property="og:description"]');
  const twitterTitle = document.querySelector<HTMLMetaElement>('meta[name="twitter:title"]');
  const twitterDescription = document.querySelector<HTMLMetaElement>('meta[name="twitter:description"]');
  const canonical = document.querySelector<HTMLLinkElement>('link[rel="canonical"]');
  if (!description || !robots) return;
  const isPublic = route !== 'app' && route !== 'notFound';
  const values = isPublic
    ? metadata[route]
    : { title: (title ?? 'Кабинет') + ' — KPDesk', description: 'Рабочий кабинет KPDesk.' };
  document.title = values.title;
  description.content = values.description;
  robots.content = route === 'privacy' ? 'noindex,follow' : isPublic ? 'index,follow' : 'noindex,nofollow';
  if (ogTitle) ogTitle.content = values.title;
  if (ogDescription) ogDescription.content = values.description;
  if (twitterTitle) twitterTitle.content = values.title;
  if (twitterDescription) twitterDescription.content = values.description;
  // Never expose a workspace URL as a public canonical or emit localhost into production.
  const path = isPublic ? routePaths[route] : null;
  if (path && window.location.protocol === 'https:' && window.location.hostname === 'kpdesk.ru') {
    const link = canonical ?? document.createElement('link');
    link.rel = 'canonical';
    link.href = 'https://kpdesk.ru' + path;
    if (!link.isConnected) document.head.append(link);
  } else {
    canonical?.remove();
  }
}
