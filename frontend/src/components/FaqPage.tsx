import { useEffect, useMemo, useState } from 'react';
import { ArrowRight, CheckCircle2, ChevronDown, Search } from 'lucide-react';
import { designAssets } from '../design-assets';
import { faqGroups } from '../content/faq';
import { applyDocumentSeo } from '../seo';
import { PublicFooter, PublicHeader } from './PublicChrome';
import { MarketingCta } from './MarketingShared';
import { FaqItem, useFaqAccordion } from './FaqAccordion';

export function FaqPage() {
  const [search, setSearch] = useState('');
  const [submittedSearch, setSubmittedSearch] = useState('');
  const [category, setCategory] = useState('all');
  const [expandedGroups, setExpandedGroups] = useState<string[]>([]);
  const accordion = useFaqAccordion();

  useEffect(() => applyDocumentSeo('faq'), []);

  const results = useMemo(() => faqGroups
    .filter((group) => category === 'all' || group.id === category)
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        const query = submittedSearch.trim().toLocaleLowerCase('ru-RU');
        return !query || (item.q + ' ' + item.a).toLocaleLowerCase('ru-RU').includes(query);
      }),
    })).filter((group) => group.items.length > 0), [category, submittedSearch]);

  const count = results.reduce((total, group) => total + group.items.length, 0);

  return <div className="public-page">
    <PublicHeader />
    <main>
      <section className="faq-hero" aria-labelledby="faq-title">
        <div>
          <h1 id="faq-title">Вопросы о прайсах, подготовке КП и оплате</h1>
          <p>Найдите ответ по этапу работы. Как подготовить прайс, выбрать товар при неполной заявке, проверить PDF или продлить тариф для команды.</p>
          <div className="public-hero-cta">
            <a className="button button-primary" href="/demo">Попробовать демо <ArrowRight size={18} aria-hidden="true"/></a>
            <a className="button button-white" href="#faq-search">Найти ответ</a>
          </div>
          <ul className="public-proof">
            <li><CheckCircle2 size={18} aria-hidden="true"/> Загрузка прайса</li>
            <li><CheckCircle2 size={18} aria-hidden="true"/> Подбор и генерация КП</li>
            <li><CheckCircle2 size={18} aria-hidden="true"/> Работа команды</li>
          </ul>
        </div>
        <img src={designAssets.faqHero} alt="Иллюстрация раздела помощи KPDesk"
          width={1672} height={941} decoding="async"/>
      </section>

      <section className="faq-content public-section" aria-label="Вопросы и ответы">
        <form id="faq-search" className="faq-search" role="search" onSubmit={(event) => {
          event.preventDefault();
          setSubmittedSearch(search);
        }}>
          <label className="sr-only" htmlFor="faq-query">Поиск по вопросам</label>
          <Search size={20} aria-hidden="true"/>
          <input id="faq-query" type="search" value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Например, как загрузить прайс" />
          <button className="button button-primary" type="submit">Найти</button>
        </form>
        <div className="faq-category-tabs" role="group" aria-label="Категория вопросов">
          <button className={category === 'all' ? 'active' : ''} type="button"
            aria-pressed={category === 'all'} onClick={() => setCategory('all')}>Все вопросы</button>
          {faqGroups.map((group) => <button type="button" key={group.id}
            className={category === group.id ? 'active' : ''}
            aria-pressed={category === group.id} onClick={() => setCategory(group.id)}>
            {group.title}
          </button>)}
        </div>
        <p className="faq-results-count" role="status">
          {submittedSearch || category !== 'all' ? 'Найдено вопросов: ' + count : 'Разделы справки'}
        </p>
        {results.length ? <div className="faq-groups">
          {results.map((group) => <section className="faq-group-card" key={group.id}>
            <div className="faq-group-heading">
              <h2><img src={group.icon} alt="" width={44} height={44} loading="lazy"/>
                {group.title}</h2>
              <button type="button" className="faq-group-more" aria-expanded={expandedGroups.includes(group.id)}
                aria-controls={'faq-group-' + group.id} aria-label={'Все вопросы: ' + group.title}
                onClick={() => setExpandedGroups(current => current.includes(group.id)
                  ? current.filter(id => id !== group.id) : [...current, group.id])}>
                {group.items.length} вопросов <ChevronDown size={16} aria-hidden="true"/>
              </button>
            </div>
            <div className="faq-questions" id={'faq-group-' + group.id}>
              {group.items.map((item, index) => <FaqItem key={item.q} item={item} accordion={accordion}
                hidden={index >= 3 && !submittedSearch && category === 'all' && !expandedGroups.includes(group.id)}/>)}
            </div>
          </section>)}
        </div> : <div className="faq-no-results">
          <h2>Ответ не найден</h2>
          <p>Попробуйте короткий запрос: «прайс», «ИИ» или «PDF». Можно также открыть нужную категорию.</p>
          <button className="button button-white" type="button" onClick={() => {
            setSearch(''); setSubmittedSearch(''); setCategory('all');
          }}>Сбросить поиск</button>
        </div>}
      </section>

      <section className="faq-help-cta public-section">
        <div className="faq-help-copy"><img src={designAssets.icon.faq} width={64} height={64} alt="" loading="lazy"/><div><h2>Не нашли ответ на свой вопрос?</h2>
          <p>Напишите на <a href="mailto:support@kpdesk.ru">support@kpdesk.ru</a> и расскажите, на каком шаге нужна помощь.</p>
        </div></div>
        <a href="mailto:support@kpdesk.ru" className="button button-primary">Связаться с поддержкой <ArrowRight size={18} aria-hidden="true"/></a>
      </section>
      <section className="public-section faq-resources">
        <h2>С чего начать</h2>
        <div className="faq-resource-grid">
          {[
            { title: 'Как создать КП', text: 'Прайс, разбор заявки, подбор и генерация текста', href: '/how-it-works', icon: designAssets.icon.documentInfo },
            { title: 'Демо', text: 'Попробуйте подбор и расчёт на учебном прайсе', href: '/demo', icon: designAssets.icon.video },
            { title: 'Возможности KPDesk', text: 'Что автоматизировано и где нужен ваш выбор', href: '/features', icon: designAssets.icon.quotes },
          ].map(item => <a className="faq-resource" href={item.href} key={item.title}>
            <img src={item.icon} alt="" width={48} height={48} loading="lazy"/>
            <span><strong>{item.title}</strong><span>{item.text}</span></span>
            <ArrowRight size={18} aria-hidden="true"/>
          </a>)}
        </div>
      </section>
      <MarketingCta title="Переходите к своему первому КП"
        description="Добавьте прайс и заявку клиента, проверьте подбор и подготовьте предложение в PDF."/>
    </main>
    <PublicFooter />
  </div>;
}
