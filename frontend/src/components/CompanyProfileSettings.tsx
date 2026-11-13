import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type { CompanyProfileData, CompanyProfileSnapshot, PartyDetails } from '../tenant-session';
import { CompanyInnLookup, formatPartyDetails } from './CompanyInnLookup';

const fields = [
  ['about', 'Описание компании', 1000], ['advantages', 'Подтверждённые преимущества', 1000],
  ['contacts', 'Контакты для КП', 600], ['default_terms', 'Стандартные условия', 1000],
  ['generation_style', 'Пожелания к тексту', 500],
] as const;
const empty = (name: string): CompanyProfileData => ({ schema_version: 1, display_name: name,
  about: '', advantages: '', contacts: '', default_terms: '', generation_style: '' });

export function CompanyProfileSettings({ organizationId, organizationName, csrf, canManage, onSaved }: {
  organizationId: string; organizationName: string; csrf: string | null;
  canManage: boolean; onSaved: () => void;
}) {
  const [profile, setProfile] = useState<CompanyProfileSnapshot | null>(null);
  const [draft, setDraft] = useState(() => empty(organizationName));
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [logoBusy, setLogoBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const epoch = useRef(0);
  const previousLoad = useRef<{ org: string; reload: number } | null>(null);
  const scope = JSON.stringify([organizationId, csrf, canManage]);
  const scopeRef = useRef(scope); scopeRef.current = scope;
  useEffect(() => {
    const preserveDraft = canManage && dirty && previousLoad.current?.org === organizationId
      && previousLoad.current.reload === reload;
    previousLoad.current = { org: organizationId, reload };
    const version = ++epoch.current;
    setBusy(false);
    setLogoBusy(false);
    setLoading(true); setAvailable(false); setError(''); setNotice(''); setReviewed(false);
    void sessionApi.companyProfile(organizationId).then(result => {
      if (version !== epoch.current) return;
      if (!preserveDraft) {
        setProfile(result.profile); setDraft(result.profile?.data ?? empty(organizationName)); setDirty(false);
      }
      setAvailable(true);
    }).catch(cause => {
      if (version === epoch.current) setError(cause instanceof Error ? cause.message : 'Не удалось загрузить сведения компании.');
    }).finally(() => { if (version === epoch.current) setLoading(false); });
    return () => { epoch.current++; };
  }, [organizationId, organizationName, reload, scope]);
  const change = (field: 'display_name' | typeof fields[number][0], value: string) => {
    setDraft(current => ({ ...current, [field]: value }));
    setReviewed(false); setDirty(true); setNotice('');
  };
  const applyParty = (party: PartyDetails) => {
    if (!canManage || !csrf || !available || loading || busy) return;
    if (party.short_name.length > 200 || !party.short_name.trim()) {
      setError('Название из реестра должно содержать от 1 до 200 символов. Внесите название и реквизиты вручную. Черновик не изменён.');
      return;
    }
    setDraft(current => ({ ...current, display_name: party.short_name, issuer_details: party }));
    setReviewed(false); setDirty(true); setNotice(''); setError('');
  };
  const save = async () => {
    if (!csrf || !canManage || !available || loading || busy || !reviewed) return;
    const sentScope = scopeRef.current;
    const version = ++epoch.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await sessionApi.saveCompanyProfile(organizationId, draft, profile?.revision ?? 0, csrf);
      if (version !== epoch.current || sentScope !== scopeRef.current) return;
      if (result.profile.revision !== (profile?.revision ?? 0) + 1) throw new Error('Получена другая версия настроек компании. Загрузите сохранённые сведения.');
      setProfile(result.profile); setDraft(result.profile.data); setReviewed(false); setDirty(false);
      setNotice('Настройки сохранены. Эти сведения будут использоваться в новых предложениях.');
      onSaved();
    } catch (cause) {
      if (version === epoch.current && sentScope === scopeRef.current)
        setError(cause instanceof Error ? cause.message : 'Не удалось сохранить настройки.');
    } finally { if (version === epoch.current && sentScope === scopeRef.current) setBusy(false); }
  };
  const uploadLogo = async (file: File) => {
    if (!csrf || !canManage || !available || loading || busy) return;
    if (file.size > 1024 * 1024 || !file.size) {
      setError('Выберите непустой PNG, JPEG или WebP размером не больше 1 МиБ. Черновик не изменён.');
      return;
    }
    const sentScope = scopeRef.current;
    const version = ++epoch.current;
    setBusy(true); setLogoBusy(true); setError(''); setNotice('');
    try {
      const result = await sessionApi.prepareCompanyLogo(organizationId, file, csrf);
      if (version !== epoch.current || sentScope !== scopeRef.current) return;
      setDraft(current => ({ ...current, logo: result.logo }));
      setReviewed(false); setDirty(true);
      setNotice('Логотип подготовлен. Проверьте его и сохраните настройки компании.');
    } catch (cause) {
      if (version === epoch.current && sentScope === scopeRef.current)
        setError(`${cause instanceof Error ? cause.message : 'Не удалось загрузить логотип.'} Черновик не изменён.`);
    } finally {
      if (version === epoch.current && sentScope === scopeRef.current) { setBusy(false); setLogoBusy(false); }
    }
  };
  return <section aria-label="Сведения компании для КП">
    <h3>Сведения компании для КП</h3>
    <p>Заполните один раз: ИИ использует описание и преимущества в новых КП.
      Контакты и реквизиты отправителя попадут в PDF отдельными блоками. Сведения проверяет руководитель.</p>
    {loading && <p role="status">Загружаем настройки компании…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!loading && <button type="button" className="text-button" disabled={busy}
      onClick={() => setReload(value => value + 1)}>Загрузить сохранённую версию</button>}
    {dirty && <p>Загрузка сохранённой версии заменит несохранённые правки.</p>}
    {!loading && available && (canManage ? <div className="access-form">
      <label>Логотип компании
        <input type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" disabled={busy || !csrf}
          onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void uploadLogo(file); }}/>
      </label>
      <p>PNG, JPEG или WebP до 1 МиБ. Логотип будет добавлен в КП целиком, с сохранением пропорций.</p>
      <p>По умолчанию используется логотип KPDesk. Загрузите свой, чтобы заменить его в КП.</p>
      {logoBusy && <p role="status">Обрабатываем логотип…</p>}
      <div className="company-logo-preview">
        <img src={draft.logo ? `data:image/png;base64,${draft.logo.data_base64}` : '/assets/kpdesk-default-v1.png'}
          alt={draft.logo ? 'Логотип компании' : 'Логотип KPDesk по умолчанию'}/>
        {draft.logo && <button type="button" className="text-button" disabled={busy} onClick={() => {
          setDraft(current => ({ ...current, logo: undefined }));
          setReviewed(false); setDirty(true); setNotice('');
        }}>Убрать логотип</button>}
      </div>
      <CompanyInnLookup organizationId={organizationId} csrf={csrf} disabled={busy} onApply={applyParty}/>
      {draft.issuer_details && <section aria-label="Реквизиты отправителя" className="company-requisites-confirmed">
        <h4>Реквизиты отправителя</h4>
        <pre>{formatPartyDetails(draft.issuer_details)}</pre>
        <p>Сохраняются отдельно от контактов и включаются в версию КП. Для замены найдите другую компанию по ИНН.</p>
        <button type="button" className="text-button" disabled={busy} onClick={() => {
          setDraft(current => ({ ...current, issuer_details: undefined }));
          setReviewed(false); setDirty(true); setNotice('');
        }}>Убрать реквизиты отправителя</button>
      </section>}
      <label>Название отправителя
        <input value={draft.display_name} maxLength={200} disabled={busy}
          onChange={event => change('display_name', event.target.value)}/>
      </label>
      {fields.map(([field, label, limit]) => <label key={field}>{label}
        <textarea rows={3} maxLength={limit} value={draft[field]} disabled={busy}
          onChange={event => change(field, event.target.value)}/>
      </label>)}
      <p>Укажите проверенные сведения о компании. Цены и условия подбора настраиваются отдельно при подготовке КП.</p>
      <label className="tenant-checkbox"><input type="checkbox" checked={reviewed} disabled={busy}
        onChange={event => setReviewed(event.target.checked)}/>
        Подтверждаю достоверность сведений и разрешаю использовать их в коммерческих предложениях</label>
      <button type="button" className="button button-primary"
        disabled={!csrf || busy || !reviewed || !draft.display_name.trim()} onClick={() => void save()}>
        {logoBusy ? 'Обрабатываем логотип…' : busy ? 'Сохраняем…' : 'Сохранить настройки компании'}</button>
    </div> : profile ? <dl className="workspace-company-readonly">{[['display_name', 'Отправитель'] as const, ...fields].map(([field, label]) =>
      <div key={field}><dt>{label}</dt><dd>{profile.data[field] || 'Не указано'}</dd></div>)}
      {profile.data.issuer_details && <div><dt>Реквизиты отправителя</dt><dd><pre>{formatPartyDetails(profile.data.issuer_details)}</pre></dd></div>}</dl>
      : <p>Руководитель ещё не сохранил сведения компании.</p>)}
    {profile && available && <p>Сохранённая версия: {profile.revision}.</p>}
    {available && !canManage && <div className="company-logo-preview">
      <img src={profile?.data.logo ? `data:image/png;base64,${profile.data.logo.data_base64}` : '/assets/kpdesk-default-v1.png'}
        alt={profile?.data.logo ? 'Логотип компании' : 'Логотип KPDesk по умолчанию'}/>
    </div>}
  </section>;
}
