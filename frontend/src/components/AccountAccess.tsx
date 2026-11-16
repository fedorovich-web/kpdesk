import { useEffect, useRef, useState } from 'react';
import '../styles.css';
import type { FormEvent } from 'react';
import { ArrowRight, CheckCircle2, LockKeyhole, Mail, UploadCloud } from 'lucide-react';
import { designAssets } from '../design-assets';
import { TenantOperations } from './TenantOperations';
import { WorkspaceNavigation } from './WorkspaceNavigation';
import { BillingPanel } from './BillingPanel';
import { sessionApi, SessionApiError } from '../tenant-session';
import type { ActiveTenantCatalog, CatalogPreflight, TenantOrganization } from '../tenant-session';

type Mode = 'login' | 'register' | 'resend-confirmation' | 'verify-email'
  | 'forgot-password' | 'reset-password' | 'join' | 'organizations';

const paths: Record<Mode, string> = {
  login: '/auth/login',
  register: '/auth/register',
  'resend-confirmation': '/auth/resend-confirmation',
  'verify-email': '/auth/verify-email',
  'forgot-password': '/auth/forgot-password',
  'reset-password': '/auth/reset-password',
  join: '/auth/join',
  organizations: '/app',
};
const titles: Record<Mode, string> = {
  login: 'Вход в KPDesk',
  register: 'Создать аккаунт',
  'resend-confirmation': 'Повторное подтверждение почты',
  'verify-email': 'Подтверждение почты',
  'forgot-password': 'Восстановление доступа',
  'reset-password': 'Новый пароль',
  join: 'Принять приглашение',
  organizations: 'Личный кабинет KPDesk',
};
const initialMode = (): Mode => {
  if (window.location.pathname === '/auth/organizations'
    || window.location.pathname === '/app'
    || window.location.pathname.startsWith('/app/')) {
    return 'organizations';
  }
  const current = Object.entries(paths).find(([, path]) => window.location.pathname === path);
  return current ? current[0] as Mode : 'login';
};
const feedback = (error: unknown): string =>
  error instanceof Error ? error.message : 'Не удалось выполнить операцию.';

export function AccountAccess() {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [token] = useState(() => new URLSearchParams(window.location.search).get('token') ?? '');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [organizationName, setOrganizationName] = useState('');
  const [csrf, setCsrf] = useState<string | null>(null);
  const [organizations, setOrganizations] = useState<TenantOrganization[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const catalogRequestSequence = useRef(0);
  const authRequestSequence = useRef(0);
  // Bind every rendered catalog to the organization that authorized its read.
  const [catalogRecord, setCatalogRecord] = useState<{
    organizationId: string; catalog: ActiveTenantCatalog;
  } | null>(null);
  const catalog = catalogRecord?.organizationId === selectedId ? catalogRecord.catalog : null;
  const [file, setFile] = useState<File | null>(null);
  const [preflight, setPreflight] = useState<{ file: File; org: string; result: CatalogPreflight } | null>(null);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const fileRef = useRef<File | null>(null);
  fileRef.current = file;
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const secure = window.location.protocol === 'https:';
  const selectedOrganization = organizations.find(org => org.id === selectedId);

  useEffect(() => {
    // Verification secrets do not remain in history, bookmarks or copied URLs.
    if (token && (window.location.pathname === paths['verify-email']
      || window.location.pathname === paths['reset-password']
      || window.location.pathname === paths.join)) {
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, [token]);
  useEffect(() => {
    const back = () => {
      authRequestSequence.current++;
      setMode(initialMode());
      setPending(false);
      setError('');
      setNotice('');
      setPassword('');
    };
    window.addEventListener('popstate', back);
    return () => {
      authRequestSequence.current++;
      window.removeEventListener('popstate', back);
    };
  }, []);
  useEffect(() => {
    if (mode !== 'organizations' || !secure || csrf) return;
    let cancelled = false;
    void sessionApi.resume().then(result => {
      if (!cancelled) setCsrf(result.csrf_token);
    }).catch(err => {
      if (cancelled) return;
      if (err instanceof SessionApiError && err.status === 401) {
        navigate('login');
        setError('Сессия завершилась. Войдите снова.');
      } else setError(feedback(err));
    });
    return () => { cancelled = true; };
  }, [mode, secure, csrf]);
  useEffect(() => {
    if (mode !== 'organizations' || !secure || !csrf) return;
    let cancelled = false;
    void sessionApi.organizations().then(result => {
      if (!cancelled) {
        setOrganizations(result.organizations);
        setSelectedId(current => result.organizations.some(org => org.id === current)
          ? current : result.organizations[0]?.id ?? '');
      }
    }).catch(err => {
      if (cancelled) return;
      if (err instanceof SessionApiError && err.status === 401) {
        invalidateSession();
      } else setError(feedback(err));
    });
    return () => { cancelled = true; };
  }, [mode, secure, csrf]);
  useEffect(() => {
    if (!selectedId || !secure || !csrf || mode !== 'organizations') return;
    let cancelled = false;
    const sequence = ++catalogRequestSequence.current;
    void sessionApi.currentCatalog(selectedId).then(found => {
      if (!cancelled && selectedIdRef.current === selectedId
        && catalogRequestSequence.current === sequence) {
        setCatalogRecord({ organizationId: selectedId, catalog: found });
      }
    }).catch(err => {
      if (cancelled || selectedIdRef.current !== selectedId
        || catalogRequestSequence.current !== sequence) return;
      if (err instanceof SessionApiError && err.code === 'no_active_catalog') {
        setCatalogRecord(null);
      } else setError(feedback(err));
    });
    return () => { cancelled = true; };
  }, [selectedId, mode, secure, csrf]);

  const navigate = (target: Mode) => {
    authRequestSequence.current++;
    window.history.pushState({}, '', paths[target]);
    setMode(target);
    setPending(false);
    setError('');
    setNotice('');
    setPassword('');
  };

  const invalidateSession = () => {
    catalogRequestSequence.current++;
    setCsrf(null);
    setOrganizations([]);
    setSelectedId('');
    setCatalogRecord(null);
    setFile(null);
    setPreflight(null);
    setPublishConfirmed(false);
    navigate('login');
    setError('Сессия завершилась. Войдите снова.');
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!secure || pending) return;
    const sequence = ++authRequestSequence.current;
    const current = () => sequence === authRequestSequence.current;
    setPending(true);
    setError('');
    setNotice('');
    try {
      if (mode === 'register') {
        await sessionApi.register(email, password, organizationName);
        if (!current()) return;
        setNotice('Если регистрация прошла успешно, на указанную почту придёт письмо с подтверждением.');
      } else if (mode === 'resend-confirmation') {
        await sessionApi.resend(email, password);
        if (!current()) return;
        setNotice('Если аккаунт ожидает подтверждения, письмо будет отправлено повторно.');
      } else if (mode === 'login') {
        const issued = await sessionApi.login(email, password);
        if (!current()) return;
        setCsrf(issued.csrf_token);
        const visible = await sessionApi.organizations();
        if (!current()) return;
        setOrganizations(visible.organizations);
        setSelectedId(visible.organizations[0]?.id ?? '');
        navigate('organizations');
      } else if (mode === 'verify-email') {
        const confirmed = await sessionApi.verify(token);
        if (!current()) return;
        navigate('login');
        setNotice('Почта подтверждена, компания добавлена. Войдите в аккаунт, чтобы начать работу.');
        setSelectedId(confirmed.organization_id);
      } else if (mode === 'forgot-password') {
        await sessionApi.forgot(email);
        if (!current()) return;
        setNotice('Если аккаунт существует, письмо для восстановления будет отправлено.');
      } else if (mode === 'reset-password') {
        await sessionApi.reset(token, password);
        if (!current()) return;
        navigate('login');
        setNotice('Пароль изменён. Все прежние сессии завершены.');
      } else if (mode === 'join') {
        await sessionApi.joinInvitation(token, password);
        if (!current()) return;
        navigate('login');
        setNotice('Аккаунт подтверждён. Вы добавлены в приглашённую организацию.');
      }
    } catch (err) {
      if (current()) setError(feedback(err));
    } finally {
      if (current()) {
        setPending(false);
        setPassword('');
      }
    }
  };

  const inspectCatalog = async () => {
    if (!selectedId || !file || !csrf || !secure || pending) return;
    const organizationId = selectedId;
    const sourceFile = file;
    const sequence = ++catalogRequestSequence.current;
    setPreflight(null);
    setPublishConfirmed(false);
    setPending(true);
    setNotice('');
    setError('');
    try {
      const result = await sessionApi.preflightCatalog(organizationId, sourceFile, csrf);
      if (selectedIdRef.current !== organizationId || fileRef.current !== sourceFile
        || catalogRequestSequence.current !== sequence) return;
      setPreflight({ file: sourceFile, org: organizationId, result });
      setNotice(result.publishable
        ? 'Проверка завершена. Подтвердите загрузку товаров в каталог.'
        : 'В прайсе обнаружены ошибки. Исправьте исходный файл перед загрузкой.');
    } catch (err) {
      if (selectedIdRef.current === organizationId && fileRef.current === sourceFile
        && catalogRequestSequence.current === sequence) setError(feedback(err));
    } finally { setPending(false); }
  };

  const upload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedId || !file || !csrf || !secure || pending || !publishConfirmed
      || !preflight?.result.publishable || preflight.file !== file
      || preflight.org !== selectedId) return;
    const organizationId = selectedId;
    const sourceFile = file;
    const sequence = ++catalogRequestSequence.current;
    setPending(true);
    setNotice('');
    setError('');
    try {
      const published = await sessionApi.importCatalog(
        organizationId, sourceFile, csrf, crypto.randomUUID(),
        preflight.result.source_sha256,
      );
      if (selectedIdRef.current !== organizationId || fileRef.current !== sourceFile
        || catalogRequestSequence.current !== sequence) return;
      if (published.source_sha256 !== preflight.result.source_sha256
        || published.rows_count !== preflight.result.rows_count) {
        setPreflight(null);
        throw new Error('Загруженный прайс не совпадает с проверенным файлом. Проверьте активный прайс перед повторной загрузкой.');
      }
      setCatalogRecord({ organizationId, catalog: published });
      setFile(null);
      setPreflight(null);
      setPublishConfirmed(false);
      setNotice('Прайс загружен. Товаров: ' + published.rows_count);
    } catch (err) {
      if (selectedIdRef.current === organizationId
        && catalogRequestSequence.current === sequence) setError(feedback(err));
    } finally { setPending(false); }
  };

  const acceptReviewedSupplier = (normalized: File, organizationId: string) => {
    if (selectedIdRef.current !== organizationId || !csrf || mode !== 'organizations') return;
    catalogRequestSequence.current++;
    setFile(normalized);
    setPreflight(null);
    setPublishConfirmed(false);
    setError('');
    setNotice('CSV для импорта готов. Проверьте его в блоке «Импорт прайс-листа», затем подтвердите загрузку.');
  };

  const refreshActiveCatalog = async (): Promise<ActiveTenantCatalog> => {
    if (!selectedId) throw new Error('Организация не выбрана.');
    const organizationId = selectedId;
    const sequence = ++catalogRequestSequence.current;
    const current = await sessionApi.currentCatalog(organizationId);
    if (selectedIdRef.current !== organizationId
      || catalogRequestSequence.current !== sequence) {
      throw new Error('Организация или прайс изменились во время обновления.');
    }
    setCatalogRecord({ organizationId, catalog: current });
    return current;
  };

  const refreshOrganizations = async () => {
    try {
      const result = await sessionApi.organizations();
      if (!result.organizations.some(org => org.id === selectedIdRef.current)) {
        catalogRequestSequence.current++;
        setCatalogRecord(null);
        setFile(null);
        setPreflight(null);
        setPublishConfirmed(false);
      }
      setOrganizations(result.organizations);
      setSelectedId(current => result.organizations.some(org => org.id === current)
        ? current : result.organizations[0]?.id ?? '');
    } catch (err) {
      if (err instanceof SessionApiError && err.status === 401) invalidateSession();
      else setError(feedback(err));
    }
  };

  const logout = async () => {
    if (!csrf) return;
    setPending(true);
    try {
      await sessionApi.logout(csrf);
      catalogRequestSequence.current++;
      setCsrf(null);
      setSelectedId('');
      setOrganizations([]);
      setCatalogRecord(null);
      navigate('login');
      setNotice('Вы вышли из аккаунта.');
    } catch (err) { setError(feedback(err)); }
    finally { setPending(false); }
  };

  return <div className={mode === 'organizations' ? 'access-page workspace-page' : 'access-page'}>
    <header className="access-header">
      <a href="/" aria-label="KPDesk — главная"><img src={designAssets.logo} alt="KPDesk" width={172} height={44}/></a>
      <a href="/" className="access-home">На главную <ArrowRight size={16} aria-hidden="true" /></a>
    </header>
    <main className={mode === 'organizations' ? 'access-main access-main-workspace' : 'access-main'}>
      <section className={mode === 'organizations' ? 'panel access-panel access-panel-workspace' : 'panel access-panel'} aria-labelledby="account-title">
        <div className="access-heading">
          {mode !== 'organizations' && <span className="access-symbol"><LockKeyhole size={23} aria-hidden="true"/></span>}
          <h1 id="account-title">{titles[mode]}</h1>
          <p>{mode === 'organizations' ? 'Выберите компанию, загрузите прайс и подготовьте предложение по заявке клиента.'
            : 'Ваши прайсы, заявки и коммерческие предложения в одном кабинете.'}</p>
        </div>
        {!secure && <div className="alert warning" role="alert">
          Действия с аккаунтом доступны только по HTTPS. Откройте защищённую версию KPDesk.
        </div>}
        {error && <div className="alert error" role="alert">{error}</div>}
        {notice && <div className="alert success" role="status">
          <CheckCircle2 size={19} aria-hidden="true"/>{notice}
        </div>}

        {mode !== 'organizations' ? <form className="access-form" onSubmit={event => void submit(event)}>
          {(mode === 'login' || mode === 'register' || mode === 'forgot-password'
            || mode === 'resend-confirmation') && <label>
              Электронная почта
              <input type="email" name="email" autoComplete="email"
                required maxLength={254} value={email} onChange={e => setEmail(e.target.value)}
                placeholder="name@company.ru" />
            </label>}
          {mode === 'register' && <label>
            Название организации
            <input type="text" name="organization_name" required maxLength={200}
              value={organizationName} onChange={e => setOrganizationName(e.target.value)}
              placeholder="Название компании" />
          </label>}
          {(mode === 'login' || mode === 'register' || mode === 'resend-confirmation'
            || mode === 'reset-password' || mode === 'join') && <label>
              {mode === 'reset-password' || mode === 'join' ? 'Новый пароль' : 'Пароль'}
              <input type="password" name="password" required
                minLength={mode === 'register' || mode === 'reset-password' || mode === 'join' ? 12 : 1}
                maxLength={256} autoComplete={mode === 'register' || mode === 'reset-password' || mode === 'join'
                  ? 'new-password' : 'current-password'}
                value={password} onChange={e => setPassword(e.target.value)} />
              {(mode === 'register' || mode === 'reset-password' || mode === 'join') &&
                <span className="access-hint">Не менее 12 символов.</span>}
            </label>}
          {mode === 'verify-email' && <p>
            <Mail size={17} aria-hidden="true"/> Подтвердите адрес с помощью одноразовой ссылки из письма.
          </p>}
          {(mode === 'verify-email' || mode === 'reset-password' || mode === 'join') && !token &&
            <div className="alert warning" role="alert">Не найдены данные подтверждения. Откройте полную ссылку из письма.</div>}
          <button className="button button-primary button-full" type="submit"
            disabled={!secure || pending ||
              ((mode === 'verify-email' || mode === 'reset-password' || mode === 'join') && !token)}>
            {pending ? 'Проверка…' : mode === 'login' ? 'Войти' :
              mode === 'register' ? 'Зарегистрироваться' :
              mode === 'resend-confirmation' ? 'Запросить новое письмо' :
              mode === 'verify-email' ? 'Подтвердить почту' :
              mode === 'forgot-password' ? 'Отправить ссылку' :
              mode === 'join' ? 'Создать аккаунт и присоединиться' : 'Установить пароль'}
          </button>
        </form> : <div className="access-workspace">
          <div className="workspace-context">
          {!csrf && secure && <p role="status">Восстанавливаем вход в аккаунт…</p>}
          {csrf && organizations.length ? <label>
            Организация
            <select value={selectedId} onChange={e => {
              catalogRequestSequence.current++;
              setSelectedId(e.target.value);
              setCatalogRecord(null);
              setFile(null);
              setPreflight(null);
              setPublishConfirmed(false);
            }}>
              {organizations.map(organization =>
                <option key={organization.id} value={organization.id}>
                  {organization.name} — {{ owner: 'Руководитель', editor: 'Менеджер', viewer: 'Просмотр' }[organization.role]}
                </option>)}
            </select>
          </label> : csrf && <p>Доступных организаций нет.</p>}
          {csrf && <button className="button button-white workspace-refresh" type="button"
            disabled={pending} onClick={() => void refreshOrganizations()}>
            Обновить список компаний
          </button>}
          {csrf && <button className="text-button workspace-logout" type="button"
            disabled={pending} onClick={() => void logout()}>Выйти</button>}
          </div>
          <div className={csrf && selectedOrganization ? 'workspace-layout' : 'workspace-layout-empty'}>
          {csrf && selectedOrganization && <WorkspaceNavigation canWrite={selectedOrganization.role !== 'viewer'}/>}
          <div className="workspace-content">
          {csrf && selectedOrganization && <>
            <section id="workspace-catalog" className="workspace-catalog">
            <div className="workspace-section-heading">
              <div><span className="workspace-step">Шаг 1</span>
              <h2>Активный прайс</h2>
              {catalog
                ? <p>Товаров в прайсе: <strong>{catalog.rows_count}</strong></p>
                : <p>Активный прайс пока не загружен.</p>}
              </div>
              {catalog && <span className="workspace-status"><CheckCircle2 size={15} aria-hidden="true"/>Готов к подбору</span>}
            </div>
            {selectedOrganization.role !== 'viewer' && <form className="access-form" onSubmit={event => void upload(event)}>
              <label className="workspace-file-field">Импорт прайс-листа
                <input type="file" accept=".csv,.xlsx" disabled={pending}
                  onChange={e => {
                    catalogRequestSequence.current++;
                    setFile(e.target.files?.[0] ?? null);
                    setPreflight(null);
                    setPublishConfirmed(false);
                    setNotice('');
                  }}/>
              </label>
              {file && <p className="access-hint">Выбран для проверки: <strong>{file.name}</strong></p>}
              <p className="access-hint">
                Загрузите прайс CSV (UTF-8) или Excel по шаблону KPDesk.
                Если файл не соответствует шаблону, подготовьте его в разделе «Прайсы и проверка данных».
              </p>
              {!csrf && <p className="access-hint">
                Чтобы загрузить прайс, войдите в аккаунт ещё раз.
              </p>}
              <button className="button button-white button-full" type="button"
                disabled={!csrf || !file || pending || !secure}
                onClick={() => void inspectCatalog()}>
                Проверить файл
              </button>
              {preflight && preflight.file === file && preflight.org === selectedId && <div
                className="access-catalog-state" aria-label="Результат проверки прайса">
                <p>Проверка: {preflight.result.publishable ? 'готов к загрузке' : 'нужны исправления'}.
                  {' '}Товаров: {preflight.result.rows_count}.
                  {' '}Ошибок: {preflight.result.total_errors}.</p>
                {preflight.result.errors.length > 0 && <ul>
                  {preflight.result.errors.map((issue, index) => <li key={index}>
                    {issue.row !== null ? 'Строка ' + issue.row + ': ' : ''}
                    {issue.message}{issue.field ? ' (' + issue.field + ')' : ''}
                  </li>)}
                </ul>}
                {preflight.result.total_errors > preflight.result.errors.length && <p>
                  Показаны первые {preflight.result.errors.length} ошибок.
                </p>}
                {preflight.result.publishable && <label className="tenant-checkbox">
                  <input type="checkbox" checked={publishConfirmed} disabled={pending}
                    onChange={e => setPublishConfirmed(e.target.checked)}/>
                  Я проверил файл и подтверждаю загрузку новой версии прайса
                </label>}
              </div>}
              <button className="button button-primary button-full" type="submit"
                disabled={!csrf || !file || pending || !secure || !publishConfirmed
                  || !preflight?.result.publishable || preflight.file !== file
                  || preflight.org !== selectedId}>
                <UploadCloud size={18} aria-hidden="true"/> Загрузить прайс
              </button>
            </form>}
            </section>
          </>}
          {csrf && selectedOrganization && <TenantOperations
            key={selectedOrganization.id}
            organization={selectedOrganization}
            catalog={catalog} csrf={csrf} onOrganizationChange={() => void refreshOrganizations()}
            onCatalogRefresh={refreshActiveCatalog}
            onCanonicalReady={normalized => acceptReviewedSupplier(normalized, selectedId)}
            compactNavigation
          />}
          {csrf && selectedOrganization && <div id="workspace-billing">
            <BillingPanel organization={selectedOrganization} csrf={csrf}/>
          </div>}
          {!csrf && <button className="button button-white" type="button"
            onClick={() => navigate('login')}>Войти снова для сохранения данных</button>}
          </div></div>
        </div>}

        {mode !== 'organizations' && <nav className="access-links" aria-label="Восстановление доступа">
          {mode !== 'login' && <button type="button" onClick={() => navigate('login')}>Войти</button>}
          {mode !== 'register' && <button type="button" onClick={() => navigate('register')}>Регистрация</button>}
          {mode !== 'forgot-password' && <button type="button" onClick={() => navigate('forgot-password')}>Забыли пароль?</button>}
          {mode !== 'resend-confirmation' && <button type="button" onClick={() => navigate('resend-confirmation')}>
            Отправить письмо повторно
          </button>}
        </nav>}
      </section>
      <p className="access-footnote">
        Познакомиться с подготовкой КП можно без регистрации. <a href="/demo">Открыть демонстрацию</a>
      </p>
    </main>
  </div>;
}
