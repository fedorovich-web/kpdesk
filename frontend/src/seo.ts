/** Route-specific search copy based on the implemented request-to-approved-PDF workflow. */
import { launchPlans, rubles } from './content/pricing';
export const publicSeo = {
  title: 'ИИ-генератор коммерческих предложений с подбором товаров — KPDesk',
  description: 'ИИ-генератор коммерческих предложений: подбор товаров из вашего прайса, автоматический расчёт стоимости и текст КП. Утвердите КП и скачайте PDF.',
  h1: 'ИИ-генератор коммерческих предложений с подбором товаров',
  lead: 'Загрузите прайс и добавьте заявку клиента. KPDesk автоматически предложит подходящие товары, рассчитает стоимость и сгенерирует текст КП. Проверьте позиции, утвердите предложение и скачайте PDF.',
} as const;

export const publicSections = [{
  id: 'features',
  heading: 'От заявки до текста КП и расчёта стоимости',
  items: [
    { heading: 'Разбор заявки с ИИ', body: 'Вставьте письмо клиента. ИИ выделит товары, количество и характеристики — проверьте их перед подбором.' },
    { heading: 'Товары из вашего прайса', body: 'KPDesk предложит позиции из каталога. Сравните их с требованиями заявки и подтвердите выбор.' },
    { heading: 'Автоматический расчёт', body: 'Стоимость строк и общая сумма рассчитываются по выбранным товарам, количеству и ценам из вашего прайса.' },
    { heading: 'Генерация текста КП', body: 'ИИ подготовит текст по товарам, задаче клиента и условиям продажи. Проверьте предложение, утвердите его и скачайте PDF.' },
  ],
}] as const;

export const metadata = {
  public: publicSeo,
  faq: {
    title: 'Вопросы и ответы о создании КП — KPDesk',
    description: 'Ответы о KPDesk: разбор заявки с ИИ, подбор товаров из прайса, генерация текста КП и PDF после утверждения. Тарифы, работа команды и обработка данных.',
  },
  features: {
    title: 'Автоматический подбор товаров и ИИ-генерация текста КП — KPDesk',
    description: 'ИИ разбирает заявку и генерирует текст КП, KPDesk предлагает товары из вашего прайса и считает стоимость. Проверьте предложение и скачайте PDF с логотипом.',
  },
  how: {
    title: 'Как создать КП: от заявки до PDF с помощью ИИ — KPDesk',
    description: 'Загрузите прайс, разберите заявку с ИИ и подтвердите предложенные товары. KPDesk рассчитает стоимость, ИИ подготовит текст. Проверьте КП, утвердите и скачайте PDF.',
  },
  integrations: {
    title: 'Прайсы Excel и CSV, выгрузки из 1С — KPDesk',
    description: 'Загрузите прайс CSV или Excel (.xlsx) для подбора товаров и расчёта КП. Подготовьте выгрузки из 1С, CRM или Google Sheets в формате KPDesk.',
  },
  pricing: {
    title: 'Тарифы KPDesk для генерации КП и подбора товаров',
      description: `Тарифы KPDesk: ${launchPlans.map(plan => `${plan.name} ${rubles(plan.priceRub)}`).join(', ')} в месяц за организацию. Сравните условия к запуску подписок. Оплата пока не подключена.`,
  },
  demo: {
    title: 'Демо сервиса для создания КП — KPDesk',
    description: 'Попробуйте подбор товаров и расчёт КП на учебном прайсе KPDesk. Демо работает в браузере, не вызывает внешний ИИ и не требует регистрации.',
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
