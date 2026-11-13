import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  AlertCircle, ArrowRight, CheckCircle2,
  CircleHelp, ClipboardCheck, FileSpreadsheet, House, ListChecks,
  Search,
} from 'lucide-react';
import { pageFromPath, pagePaths, pageTitle } from './navigation';
import { applyDocumentSeo } from './seo';
import type { Page } from './navigation';
import { RequestError } from './api';
import { activateSampleCatalog, demoApi as api, exampleRequestText, resetDemoCatalog } from './demo/api';
import { RequestPage } from './components/RequestPage';
import { WorkflowSteps } from './components/WorkflowSteps';
import { designAssets } from './design-assets';
import { Dashboard } from './components/Dashboard';
import { CatalogPage } from './components/CatalogPage';
import { ReviewPage } from './components/ReviewPage';
import { PreviewPage } from './components/PreviewPage';
import { DemoTour, demoTourSteps } from './components/DemoTour';
import { MobileNavigation } from './components/MobileNavigation';
import { ImportCatalogPage, catalogFileError } from './components/ImportCatalogPage';
import { MODELS } from './domain';
import type {
  AutoDraft, Catalog, CatalogProduct, ConfirmedInstructions, ExtractedLine,
  ExtractedRequest, InstructionProposal,
} from './domain';

type Busy = 'initial' | 'import' | 'extract' | 'draft' | 'rules' | 'tour' | null;

interface TourSnapshot {
  page: Page; catalog: Catalog | null; items: CatalogProduct[]; requestText: string;
  model: string; extracted: ExtractedRequest | null; rulesText: string;
  rulesProposal: InstructionProposal | null; confirmedRules: ConfirmedInstructions | null;
  draft: AutoDraft | null; query: string; error: string | null; notice: string | null;
  connectionError: string | null; file: File | null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Не удалось выполнить действие. Попробуйте ещё раз.';
}
function newManualLine(): ExtractedLine {
  return {
    source_quote: '', description: '', category: null, quantity: null, unit: null,
    brand: null, paper_format: null, paper_gsm: null,
    pen_ink_color: null, pen_ink_type: null, attributes: {},
  };
}
const menu: { page: keyof typeof pagePaths; label: string; icon: typeof House }[] = [
  { page: 'home', label: 'Обзор демо', icon: House },
  { page: 'request', label: 'Создать КП', icon: ListChecks },
  { page: 'review', label: 'Проверка КП', icon: ClipboardCheck },
  { page: 'prices', label: 'Прайс-лист', icon: FileSpreadsheet },
  { page: 'import', label: 'Загрузить прайс', icon: FileSpreadsheet },
];

function PageHeading({ title, subtitle, actions }: {
  title: string; subtitle: string; actions?: React.ReactNode;
}) {
  return <div className="heading-row">
    <div><h1>{title}</h1><p>{subtitle}</p></div>
    {actions && <div className="heading-actions">{actions}</div>}
  </div>;
}

export function App() {
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname));
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [mlAvailable, setMlAvailable] = useState(false);
  const [items, setItems] = useState<CatalogProduct[]>([]);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>('initial');
  const [query, setQuery] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [requestText, setRequestText] = useState(exampleRequestText);
  const [model, setModel] = useState<string>(MODELS[0].value);
  const [extracted, setExtracted] = useState<ExtractedRequest | null>(null);
  const [rulesText, setRulesText] = useState('');
  const [rulesProposal, setRulesProposal] = useState<InstructionProposal | null>(null);
  const [confirmedRules, setConfirmedRules] = useState<ConfirmedInstructions | null>(null);
  const [draft, setDraft] = useState<AutoDraft | null>(null);
  const [tourActive, setTourActive] = useState(false);
  const [automaticTourPending, setAutomaticTourPending] = useState(() => /^\/demo\/?$/.test(window.location.pathname));
  const mounted = useRef(true);
  const tourPreparation = useRef(0);
  const tourSnapshot = useRef<TourSnapshot | null>(null);
  const tourExample = useRef<{ extracted: ExtractedRequest; draft: AutoDraft;
    resolved: ExtractedRequest; resolvedDraft: AutoDraft } | null>(null);
  const inputId = useId();

  const loadCatalog = useCallback(async () => {
    const [current, products] = await Promise.allSettled([api.catalog(), api.items()]);
    const productsError = products.status === 'rejected' ? errorMessage(products.reason) : null;
    if (current.status === 'fulfilled') {
      setCatalog(current.value);
      setConnectionError(productsError);
    } else if (current.reason instanceof RequestError && current.reason.code === 'no_active_catalog') {
      setCatalog(null);
      setConnectionError(productsError);
    } else {
      setCatalog(null);
      setConnectionError(errorMessage(current.reason));
    }
    if (products.status === 'fulfilled') setItems(products.value.items);
    else setItems([]);
  }, []);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    async function initialize() {
      const status = await Promise.allSettled([api.catalog(), api.items(), api.capabilities()]);
      if (!active) return;
      const itemsError = status[1].status === 'rejected' ? errorMessage(status[1].reason) : null;
      if (status[0].status === 'fulfilled') {
        setCatalog(status[0].value);
        setConnectionError(itemsError);
      } else if (status[0].reason instanceof RequestError && status[0].reason.code === 'no_active_catalog') {
        setCatalog(null);
        setConnectionError(itemsError);
      } else setConnectionError(errorMessage(status[0].reason));
      if (status[1].status === 'fulfilled') setItems(status[1].value.items);
      setMlAvailable(status[2].status === 'fulfilled' && status[2].value.model_trained);
      setBusy(null);
    }
    void initialize();
    return () => { active = false; mounted.current = false; tourPreparation.current++; };
  }, []);

  useEffect(() => {
    const onPopState = () => {
      tourPreparation.current++;
      setAutomaticTourPending(false);
      setPage(pageFromPath(window.location.pathname));
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);
  useEffect(() => { applyDocumentSeo(page === 'home' ? 'demo' : 'app', pageTitle(page)); }, [page]);

  const go = useCallback((target: keyof typeof pagePaths) => {
    tourPreparation.current++;
    setAutomaticTourPending(false);
    if (window.location.pathname !== pagePaths[target]) {
      window.history.pushState({}, '', pagePaths[target]);
    }
    setPage(target);
    setError(null);
    setNotice(null);
  }, []);
  const closeTour = useCallback(() => {
    const saved = tourSnapshot.current;
    if (saved) {
      if (!saved.catalog) resetDemoCatalog();
      setCatalog(saved.catalog); setItems(saved.items); setRequestText(saved.requestText);
      setModel(saved.model); setExtracted(saved.extracted); setRulesText(saved.rulesText);
      setRulesProposal(saved.rulesProposal); setConfirmedRules(saved.confirmedRules);
      setDraft(saved.draft); setQuery(saved.query); setFile(saved.file);
      setConnectionError(saved.connectionError);
      if (saved.page !== 'notFound') go(saved.page);
      setError(saved.error); setNotice(saved.notice);
    }
    tourSnapshot.current = null;
    tourExample.current = null;
    setTourActive(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('[data-demo-tour-start]')?.focus({ preventScroll: true }));
  }, [go]);
  const startTour = useCallback(async () => {
    if (busy !== null || tourActive) return;
    const preparation = ++tourPreparation.current;
    const isCurrent = () => mounted.current && tourPreparation.current === preparation;
    const saved: TourSnapshot = { page, catalog, items, requestText, model, extracted, rulesText,
      rulesProposal, confirmedRules, draft, query, file, error, notice, connectionError };
    setBusy('tour');
    try {
      await activateSampleCatalog();
      if (!isCurrent()) return;
      const [sampleCatalog, sampleItems, parsed] = await Promise.all([
        api.catalog(), api.items(), api.extract(exampleRequestText, MODELS[0].value),
      ]);
      if (!isCurrent()) return;
      const resolved: ExtractedRequest = { lines: parsed.lines.slice(0, 2) };
      const [sampleDraft, resolvedDraft] = await Promise.all([
        api.draft(sampleCatalog.id, parsed), api.draft(sampleCatalog.id, resolved),
      ]);
      if (!isCurrent()) return;
      tourSnapshot.current = saved;
      tourExample.current = { extracted: parsed, draft: sampleDraft, resolved, resolvedDraft };
      setCatalog(sampleCatalog); setItems(sampleItems.items); setQuery(''); setFile(null);
      setRequestText(exampleRequestText); setModel(MODELS[0].value); setExtracted(null);
      setRulesText(''); setRulesProposal(null); setConfirmedRules(null); setDraft(null);
      setConnectionError(null); go(demoTourSteps[0]!.page); setTourActive(true);
    } catch (cause) {
      if (!isCurrent()) return;
      if (!saved.catalog) resetDemoCatalog();
      setError(errorMessage(cause));
    } finally { if (mounted.current) setBusy(null); }
  }, [busy, tourActive, page, catalog, items, requestText, model, extracted, rulesText,
    rulesProposal, confirmedRules, draft, query, file, error, notice, connectionError, go]);
  useEffect(() => {
    if (!automaticTourPending || busy !== null) return;
    setAutomaticTourPending(false);
    if (/^\/demo\/?$/.test(window.location.pathname)) void startTour();
  }, [automaticTourPending, busy, startTour]);
  const changeTourStep = (index: number) => {
    const example = tourExample.current;
    const step = demoTourSteps[index];
    if (!example || !step) return;
    setExtracted(index < 5 ? null : index < 11 ? example.extracted : example.resolved);
    setDraft(index < 9 ? null : index < 11 ? example.draft : example.resolvedDraft);
    setRequestText(exampleRequestText); setRulesText(''); setRulesProposal(null); setConfirmedRules(null);
    setQuery(''); go(step.page);
  };

  const importFile = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file || catalogFileError(file, true)) {
      setError(file ? catalogFileError(file, true) : 'Выберите файл прайс-листа.');
      return;
    }
    setBusy('import');
    setError(null);
    setNotice(null);
    try {
      const result = await api.importCatalog(file, crypto.randomUUID());
      setFile(null);
      await loadCatalog();
      go('prices');
      setNotice('Прайс загружен. Товаров: ' + result.rows_count + '. Можно перейти к заявке.');
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const extract = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!requestText.trim()) return;
    setBusy('extract');
    setError(null);
    setNotice(null);
    setExtracted(null);
    setDraft(null);
    try {
      const result = await api.extract(requestText, model);
      if (!result.lines.length) {
        setExtracted(null);
        setError('Не удалось выделить товары. Добавьте позиции вручную.');
      } else {
        setExtracted(result);
        setNotice('В заявке ' + result.lines.length + ' позиции. Проверьте количество и характеристики.');
      }
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const handleRulesTextChange = (text: string) => {
    setRulesText(text);
    setRulesProposal(null);
    setConfirmedRules(null);
    setDraft(null);
    setError(null);
    setNotice(null);
  };
  const previewRules = async () => {
    if (!rulesText.trim()) return;
    setBusy('rules');
    setError(null);
    setRulesProposal(null);
    setConfirmedRules(null);
    try {
      const proposal = await api.previewInstructions(rulesText);
      setRulesProposal(proposal);
      if (!proposal.can_confirm) {
        setError('Не удалось применить все условия. Проверьте наценку, бюджет и ограничения по брендам: они не должны противоречить друг другу.');
      }
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const confirmRules = () => {
    if (!rulesProposal?.can_confirm || rulesProposal.raw_text !== rulesText) return;
    setConfirmedRules({ raw_text: rulesText, rules: rulesProposal.rules });
    setDraft(null);
    setNotice('Условия подтверждены. Они будут учтены при подборе и расчёте.');
    setError(null);
  };
  const automaticDraft = async () => {
    if (!extracted || !catalog || extracted.lines.length === 0) return;
    if (rulesText.trim() && !confirmedRules) {
      setError('Проверьте и подтвердите условия перед расчётом.');
      return;
    }
    if (extracted.lines.some((line) => !line.description.trim())) {
      setError('Заполните наименование каждой позиции.');
      return;
    }
    setBusy('draft');
    setError(null);
    setNotice(null);
    try {
      const result = await api.draft(catalog.id, extracted, confirmedRules ?? undefined);
      setDraft(result);
      go('review');
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const changeLine = (index: number, value: ExtractedLine) => {
    setExtracted(prev => prev ? {
      lines: prev.lines.map((line, i) => i === index ? value : line),
    } : prev);
    setDraft(null);
  };
  const handleTextChange = (text: string) => {
    setRequestText(text);
    // A request edit invalidates prior extraction and any calculated draft.
    setExtracted(null);
    setDraft(null);
    setError(null);
    setNotice(null);
  };
  const handleModelChange = (value: string) => {
    setModel(value);
    setExtracted(null);
    setDraft(null);
    setError(null);
    setNotice(null);
  };
  const addManualLine = () => {
    setExtracted(prev => {
      if (!prev) return { lines: [newManualLine()] };
      if (prev.lines.length >= 20) return prev;
      return { lines: [...prev.lines, newManualLine()] };
    });
    setDraft(null);
    setError(null);
  };
  const removeManualLine = (index: number) => {
    setExtracted(prev => prev ? { lines: prev.lines.filter((_, i) => i !== index) } : prev);
    setDraft(null);
  };
  const resetLines = () => {
    setExtracted(null);
    setDraft(null);
    setError(null);
  };
  const cancelRequest = () => {
    setRequestText(exampleRequestText);
    setExtracted(null);
    setRulesText('');
    setRulesProposal(null);
    setConfirmedRules(null);
    setDraft(null);
    go('home');
  };
  const restartDemo = () => {
    setTourActive(false);
    resetDemoCatalog();
    setCatalog(null);
    setItems([]);
    setFile(null);
    setQuery('');
    setConnectionError(null);
    cancelRequest();
  };
  const isBusy = busy === 'import' || busy === 'extract' || busy === 'draft' || busy === 'rules' || busy === 'tour';
  const unresolved = draft?.lines.some(line => line.status !== 'auto_selected') ?? false;
  const missingExamplePaper = draft?.lines[2]?.status === 'no_match'
    && extracted?.lines[2]?.paper_gsm === 120 && extracted.lines[2].paper_format === 'A4';
  return (
    <div className={'app-layout' + (tourActive ? ' demo-tour-running' : '')}>
      <aside className="sidebar" aria-label="Главная навигация">
        <a className="brand" href="/" aria-label="KPDesk — на главную"><img src={designAssets.logo} alt="KPDesk" width={203} height={52} /></a>
        <nav aria-label="Разделы">
          {menu.map(({ page: target, label, icon: Icon }) =>
            <a key={target} href={pagePaths[target]}
              className={'nav-link' + (page === target ? ' current' : '')}
              aria-current={page === target ? 'page' : undefined}
              onClick={(event) => {
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                go(target);
              }}>
              <Icon size={19} strokeWidth={1.9} aria-hidden="true"/><span>{label}</span>
            </a>)}
        </nav>
        <div className="sidebar-help">
          <span className="help-icon"><CircleHelp size={21}/></span>
          <strong>Попробуйте создать КП</strong>
          <p>Пройдите путь от прайса и заявки до подбора товаров, расчёта и PDF.</p>
          <button className="button button-white button-full" type="button" onClick={() => go('request')}>
            Новая заявка <ArrowRight size={15}/>
          </button>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <a href="/" className="app-mobile-logo" aria-label="KPDesk — на главную"><img src={designAssets.logo} alt="KPDesk" width={203} height={52}/></a>
          <div className="global-search">
            <Search size={19} aria-hidden="true" />
            <label htmlFor={inputId} className="sr-only">Поиск товаров</label>
            <input type="text" id={inputId} value={query} onChange={(event) => {
              setQuery(event.target.value);
              if (event.target.value) go('prices');
            }} placeholder="Название, артикул или бренд" />
          </div>
          <MobileNavigation links={menu.map(item => ({
            href: pagePaths[item.page], label: item.label, current: page === item.page,
          }))} onNavigate={href => {
            const target = menu.find(item => pagePaths[item.page] === href);
            if (target) go(target.page);
          }}/>
          <a className="demo-exit-link" href="/">На главную</a>
          <div className="topbar-right">
            <span className="topbar-support"><CircleHelp size={17} /> Демо KPDesk</span>
            <span className="topbar-separator" />
            <span className="avatar">KP</span>
            <span className="account">KPDesk <small>Демо · учебные данные</small></span>
          </div>
        </header>

        <main className="main-content" id="content">
          <div className="alert warning" role="status">
            Учебное демо: готовый прайс и пример заявки. Разбор и текст подготовлены заранее, ИИ здесь не вызывается.
            Работа сбросится при обновлении страницы. <a href="/app">Открыть личный кабинет</a>.
          </div>
          {tourActive ? <DemoTour page={page} busy={busy !== null} onStepChange={changeTourStep} onClose={closeTour}/> : automaticTourPending || busy === 'tour'
            ? <section className="panel demo-guide" role="status">Открываем пошаговую инструкцию…</section>
            : <section className="panel demo-guide" aria-labelledby="demo-guide-title">
            <div className="demo-guide-heading">
              <div><h2 id="demo-guide-title">От прайса до КП в PDF</h2>
                <p>Посмотрите, как из заявки с бумагой и ручками получается предложение с товарами и ценами.</p></div>
              <button type="button" className="text-button" disabled={busy !== null} onClick={restartDemo}>Начать демо заново</button>
            </div>
            <ol className="demo-guide-steps">
              <li><strong>Загрузите прайс</strong><span>Откройте пять учебных товаров или загрузите тот же CSV-файл.</span></li>
              <li><strong>Проверьте заявку</strong><span>Откройте позиции примера, задайте наценку и бюджет.</span></li>
              <li><strong>Проверьте подбор</strong><span>Сверьте товары с заявкой. Для бумаги 120 г/м² потребуется уточнение.</span></li>
              <li><strong>Сохраните PDF</strong><span>Проверьте состав и сумму, выберите оформление и откройте печать.</span></li>
            </ol>
            <p className="demo-guide-next">{!catalog
              ? 'Начните с учебного прайса или пройдите весь сценарий с подсказками.'
              : missingExamplePaper
                ? 'Бумаги 120 г/м² нет в прайсе. Для примера без неё вернитесь к заявке, удалите позицию 3 и пересчитайте. В рабочем КП такое изменение нужно согласовать с клиентом.'
                : unresolved
                  ? 'Подобраны не все товары. Сверьте позиции с учебным прайсом, исправьте заявку и повторите расчёт.'
                : draft?.budget_exceeded
                  ? 'Сумма превышает бюджет. Уточните условия, затем проверьте и подтвердите их заново.'
                  : draft?.ready_for_draft
                    ? 'Товары подобраны, сумма рассчитана. Откройте предпросмотр и сохраните PDF через печать браузера.'
                    : 'Прайс готов. Перейдите к заявке, откройте позиции примера и проверьте их перед подбором.'}</p>
            <div className="demo-guide-actions">
              <button className="button button-primary" type="button" data-demo-tour-start disabled={busy !== null} onClick={() => void startTour()}>
                Показать инструкцию <ArrowRight size={17} aria-hidden="true"/></button>
              {catalog && !draft && page !== 'request' && <button className="button button-primary" type="button" onClick={() => go('request')}>Продолжить с учебной заявкой</button>}
              {draft && page === 'home' && <button className="button button-primary" type="button" onClick={() => go('review')}>Продолжить проверку</button>}
              <a className="button button-white" href="/app">Создать КП из своего прайса</a>
            </div>
            <details><summary>Как работать с примером</summary>
              <p>Можно менять позиции и количество, задавать наценку и бюджет. Например: «Наценка 18%. Бюджет до 1100 рублей.». Подбор использует пять товаров учебного прайса. Когда все позиции подобраны и сумма укладывается в бюджет, можно сохранить PDF с отметкой «ДЕМО».</p>
              <p>В личном кабинете вы загрузите свой прайс, разберёте заявку с помощью ИИ, добавите данные компании и сохраните версии КП.</p>
            </details>
          </section>}
          {connectionError && page !== 'home' && page !== 'request' && page !== 'preview' && <div className="alert warning" role="alert">
            <AlertCircle size={20} aria-hidden="true" />
            <span>Не удалось открыть учебный прайс: {connectionError}. Загрузите его повторно, чтобы продолжить подбор.</span>
            <button className="button button-white" type="button" onClick={() => void loadCatalog()}>Повторить</button>
          </div>}
          {error && <div className="alert error" role="alert"><AlertCircle size={20} aria-hidden="true"/>{error}</div>}
          {notice && <div className="alert success" role="status"><CheckCircle2 size={20} aria-hidden="true"/>{notice}</div>}

          {page === 'home' && <Dashboard isDemo catalog={catalog} draft={draft}
            connectionError={connectionError} onNavigate={go} />}

          {page === 'notFound' && <section className="panel empty-state">
            <h1>Страница не найдена</h1>
            <p>Проверьте адрес или вернитесь на главную страницу демо.</p>
            <button className="button button-primary" type="button" onClick={() => go('home')}>На главную демо <ArrowRight size={17}/></button>
          </section>}

          {page === 'prices' && <CatalogPage isDemo
            catalog={catalog} products={items} query={query} onQueryChange={setQuery} loadError={connectionError}
            onImport={() => go('import')} />}

          {page === 'import' && <div className="panel" data-demo-target="activate">
            <p>В учебном прайсе пять товаров: бумага и ручки с артикулами и ценами. Откройте его, чтобы перейти к заявке.</p>
            <button className="button button-primary" type="button" disabled={isBusy}
              onClick={() => void (async () => {
                try {
                  setBusy('import');
                  await activateSampleCatalog();
                  await loadCatalog();
                  go('prices');
                  setNotice('Прайс из пяти товаров готов. Перейдите к заявке.');
                } catch (error) { setError(errorMessage(error)); }
                finally { setBusy(null); }
              })()}>Активировать учебный прайс</button>
          </div>}
          {page === 'import' && <ImportCatalogPage isDemo
            file={file} onFileChange={setFile} busy={busy === 'import'}
            onSubmit={(event) => void importFile(event)} onBack={() => go('prices')} />}

          {page === 'request' && <RequestPage isDemo
            catalog={catalog} catalogError={connectionError}
            requestText={requestText} model={model} extracted={extracted}
            rulesText={rulesText} rulesProposal={rulesProposal}
            rulesConfirmed={confirmedRules !== null} reviewingRules={busy === 'rules'}
            onRulesTextChange={handleRulesTextChange}
            onPreviewRules={() => void previewRules()} onConfirmRules={confirmRules}
            busy={isBusy} extracting={busy === 'extract'} drafting={busy === 'draft'}
            onTextChange={handleTextChange} onModelChange={handleModelChange}
            onExtract={(event) => void extract(event)}
            onAddManual={addManualLine} onAddLine={addManualLine}
            onChangeLine={changeLine} onRemoveLine={removeManualLine}
            onSuggestCategory={mlAvailable ? api.suggestCategory : undefined}
            onResetLines={resetLines} onDraft={() => void automaticDraft()}
            onBack={cancelRequest} onImport={() => go('import')}/>}

          {page === 'review' && <>
            <PageHeading title="Проверьте товары и расчёт" subtitle="Сверьте артикулы, количество и цены с заявкой. Уточните позиции без подходящего товара, затем откройте документ." />
            {draft && <WorkflowSteps active={3}/>}
            <ReviewPage isDemo draft={draft} extracted={extracted}
              onBack={() => go('request')} onPreview={() => go('preview')}/>
          </>}
          {page === 'preview' && <PreviewPage isDemo draft={draft} extracted={extracted}
            onBack={() => go('review')} onNewRequest={() => go('request')}/>}

        </main>
      </div>
    </div>
  );
}
