import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type {
  CommercialDocument, PresentationPreview, ProposalContext, TenantQuote, CompanyProfileSnapshot,
  DocumentAlternative, ProposalTemplateSeed, PartyDetails, DocumentAppearance,
} from '../tenant-session';
import { DocumentAlternativeSummary, QuoteDocumentAlternatives } from './QuoteDocumentAlternatives';
import { defaultDocumentAppearance, DocumentAppearanceSettings } from './DocumentAppearanceSettings';
import { CompanyInnLookup, formatPartyDetails } from './CompanyInnLookup';

type Props = {
  organizationId: string; issuerName: string; quote: TenantQuote;
  csrf: string | null; canWrite: boolean; requestBrief: string;
  onApproved: (quote: TenantQuote) => void;
  useCompanyProfile?: boolean; profileReloadToken?: number;
  templateSeed?: ProposalTemplateSeed | null;
};
const today = () => {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
};

export function QuoteProposalComposer({
  organizationId, issuerName, quote, csrf, canWrite, requestBrief, onApproved,
  useCompanyProfile = false, profileReloadToken = 0, templateSeed = null,
}: Props) {
  const [context, setContext] = useState<ProposalContext>(() => ({
    issuer_name: issuerName, recipient_name: '',
    manager_brief: requestBrief.slice(0, 2000) ||
    'Подготовь предложение по заявке клиента: опиши предлагаемые товары, их характеристики и подтверждённые условия. Предложи следующий шаг для согласования заказа.',
    commercial_terms: templateSeed?.commercial_terms ?? '',
  }));
  const [issuedOn, setIssuedOn] = useState(today);
  const [preview, setPreview] = useState<PresentationPreview | null>(null);
  const [alternatives, setAlternatives] = useState<DocumentAlternative[]>([]);
  const [appearance, setAppearance] = useState<DocumentAppearance>(() => ({
    ...(templateSeed?.appearance ?? defaultDocumentAppearance), layout_version: 2, default_logo: 'kpdesk_v1',
  }));
  const [recipientDetails, setRecipientDetails] = useState<PartyDetails | null>(null);
  const [pdfPreview, setPdfPreview] = useState<{ url: string; identity: string } | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [company, setCompany] = useState<CompanyProfileSnapshot | null>(null);
  const [profileStatus, setProfileStatus] = useState<'loading' | 'ready' | 'error'>(useCompanyProfile ? 'loading' : 'ready');
  const [profileError, setProfileError] = useState('');
  const [profileRetry, setProfileRetry] = useState(0);
  const [withoutSavedProfile, setWithoutSavedProfile] = useState(false);
  const usingProfile = useCompanyProfile && !withoutSavedProfile;
  const customized = useRef(new Set<keyof ProposalContext>(templateSeed ? ['commercial_terms'] : []));
  const epoch = useRef(0);
  const approvalKey = useRef<string | null>(null);
  const scope = JSON.stringify([organizationId, quote.quote_id, quote.revision, quote.snapshot_sha256, csrf, canWrite,
    useCompanyProfile, profileReloadToken, profileRetry, withoutSavedProfile]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const draftDocument: CommercialDocument | null = preview ? {
    ...context, issued_on: issuedOn, narrative: preview.narrative, appearance,
    ...(usingProfile ? { company_profile: preview.company_profile ?? null } : {}),
    ...(preview.alternatives?.length ? { alternatives: preview.alternatives } : {}),
    ...(recipientDetails ? { recipient_details: recipientDetails } : {}),
  } : null;
  const documentIdentity = JSON.stringify([scope, draftDocument]);
  const documentIdentityRef = useRef(documentIdentity);
  documentIdentityRef.current = documentIdentity;
  const freshPdf = pdfPreview?.identity === documentIdentity;
  useEffect(() => { setPdfPreview(null); setReviewed(false); approvalKey.current = null; }, [documentIdentity]);
  useEffect(() => () => { if (pdfPreview) URL.revokeObjectURL(pdfPreview.url); }, [pdfPreview]);
  useEffect(() => {
    epoch.current++; setPreview(null); setAlternatives([]); setReviewed(false); setConsent(false);
    setBusy(false); setError(''); approvalKey.current = null;
    return () => { epoch.current++; };
  }, [scope]);
  useEffect(() => {
    if (!usingProfile || quote.approved) { setProfileStatus('ready'); return; }
    let active = true;
    setProfileStatus('loading'); setProfileError(''); setCompany(null);
    void sessionApi.companyProfile(organizationId).then(result => {
      if (!active) return;
      setCompany(result.profile); setProfileStatus('ready');
      if (result.profile) setContext(current => ({
        ...current,
        issuer_name: customized.current.has('issuer_name') ? current.issuer_name : result.profile!.data.display_name,
        commercial_terms: customized.current.has('commercial_terms') ? current.commercial_terms : result.profile!.data.default_terms,
      }));
    }).catch(cause => {
      if (active) { setProfileStatus('error'); setProfileError(cause instanceof Error ? cause.message : 'Не удалось загрузить сведения компании.'); }
    });
    return () => { active = false; };
  }, [organizationId, usingProfile, profileReloadToken, profileRetry, quote.approved, csrf]);

  const changeContext = (field: keyof ProposalContext, value: string) => {
    customized.current.add(field);
    epoch.current++;
    setContext(current => ({ ...current, [field]: value }));
    if (field === 'recipient_name') setRecipientDetails(null);
    setConsent(false);
    setPreview(null); setReviewed(false); setBusy(false); setError('');
    approvalKey.current = null;
  };
  const generate = async () => {
    if (!csrf || !canWrite || !consent || busy || profileStatus !== 'ready') return;
    const current = ++epoch.current;
    const sentScope = scopeRef.current;
    setBusy(true); setError(''); setPreview(null); setReviewed(false);
    approvalKey.current = null;
    try {
      const result = alternatives.length
        ? await sessionApi.generatePresentation(organizationId, quote, context, csrf, consent,
          usingProfile ? company?.revision ?? 0 : undefined, alternatives.map(item => item.selection))
        : usingProfile
        ? await sessionApi.generatePresentation(organizationId, quote, context, csrf, consent, company?.revision ?? 0)
        : await sessionApi.generatePresentation(organizationId, quote, context, csrf, consent);
      if (current !== epoch.current || sentScope !== scopeRef.current) return;
      if (result.quote_id !== quote.quote_id || result.source_revision !== quote.revision
        || result.source_snapshot_sha256 !== quote.snapshot_sha256 || !result.requires_human_review) {
        throw new Error('Текст относится к другой версии. Обновите КП.');
      }
      if (usingProfile && ((result.company_profile?.revision ?? 0) !== (company?.revision ?? 0)
        || result.company_profile?.sha256 !== company?.sha256)) {
        throw new Error('Сведения компании изменились. Обновите их и подготовьте текст заново.');
      }
      if (JSON.stringify((result.alternatives ?? []).map(item => item.selection))
        !== JSON.stringify(alternatives.map(item => item.selection))) {
        throw new Error('Состав вариантов не соответствует выбранному. Повторите генерацию.');
      }
      setPreview(result);
    } catch (cause) {
      if (current === epoch.current && sentScope === scopeRef.current)
        setError(cause instanceof Error ? cause.message : 'Не удалось подготовить текст предложения.');
    } finally {
      if (current === epoch.current && sentScope === scopeRef.current) setBusy(false);
    }
  };
  const showPdf = async () => {
    if (!draftDocument || !csrf || !canWrite || busy || !issuedOn) return;
    const sentIdentity = documentIdentityRef.current;
    const sentScope = scopeRef.current;
    const current = ++epoch.current;
    setBusy(true); setReviewed(false); setError('');
    try {
      const blob = await sessionApi.previewDocumentPdf(organizationId, quote, draftDocument, csrf);
      if (current !== epoch.current || scopeRef.current !== sentScope || documentIdentityRef.current !== sentIdentity) return;
      if (!blob.size || blob.type !== 'application/pdf') throw new Error('Не удалось получить PDF для просмотра. Повторите попытку.');
      setPdfPreview({ url: URL.createObjectURL(blob), identity: sentIdentity });
    } catch (failure) {
      if (current === epoch.current && scopeRef.current === sentScope)
        setError(failure instanceof Error ? failure.message : 'Не удалось показать PDF.');
    } finally {
      if (current === epoch.current && scopeRef.current === sentScope) setBusy(false);
    }
  };
  const approve = async () => {
    if (!preview || !draftDocument || !freshPdf || !reviewed || !csrf || !canWrite || busy || !issuedOn) return;
    const current = ++epoch.current;
    const sentScope = scopeRef.current;
    setBusy(true); setError('');
    approvalKey.current ??= crypto.randomUUID();
    try {
      const saved = await sessionApi.approveDocument(
        organizationId, quote, draftDocument, csrf, approvalKey.current,
      );
      if (current !== epoch.current || sentScope !== scopeRef.current) return;
      if (!saved.approved || saved.quote_id !== quote.quote_id
        || saved.revision !== quote.revision + 1 || !saved.document || !saved.approval) {
        throw new Error('Не удалось подтвердить утверждение КП. Откройте последнюю версию и проверьте её статус перед повторной попыткой.');
      }
      onApproved(saved);
    } catch (cause) {
      if (current === epoch.current && sentScope === scopeRef.current)
        setError(cause instanceof Error ? cause.message : 'Не удалось утвердить КП.');
    } finally {
      if (current === epoch.current && sentScope === scopeRef.current) setBusy(false);
    }
  };

  if (quote.approved && quote.document) return <section aria-label="Утверждённое предложение">
    <h3>{quote.document.narrative.headline}</h3>
    <p>{quote.document.narrative.opening}</p>
    <p>{quote.document.narrative.solution}</p>
    {!!quote.document.alternatives?.length && <div className="composition-grid" role="group" aria-label="Сохранённые варианты КП">
      {quote.document.alternatives.map((alternative, index) => <article className="composition-card" key={index}>
        <DocumentAlternativeSummary alternative={alternative} title={`Вариант ${index + 2}`}/>
      </article>)}
    </div>}
    {quote.document.commercial_terms && <p>{quote.document.commercial_terms}</p>}
    <p>{quote.document.narrative.closing}</p>
    {quote.document.appearance && <p>Оформление: {quote.document.appearance.template === 'compact'
      ? 'Краткое предложение' : 'Подробное предложение'} · {quote.document.appearance.accent_color}</p>}
    <p role="status">КП утверждено. Текст, состав и стоимость сохранены в версии {quote.revision}.</p>
  </section>;
  if (!canWrite || !quote.ready_for_draft) return null;
  return <section className="tenant-quote-rules" aria-label="ИИ-генератор текста КП">
    <h3>Текст коммерческого предложения</h3>
    <p>Укажите задачу клиента и важные условия. ИИ подготовит текст по сохранённому составу,
      а вы проверите предложение перед утверждением. Изменения товаров сначала сохраните в КП.</p>
    {error && <p role="alert">{error}</p>}
    {useCompanyProfile && <div>
      {withoutSavedProfile && <p>Сохранённые сведения компании не применяются к этому КП. Используются состав и ваше задание.</p>}
      {profileStatus === 'loading' ? <p role="status">Загружаем сведения компании…</p>
        : profileStatus === 'error' ? <p role="alert">{profileError}</p>
          : !withoutSavedProfile && <p>{company ? `Используются сведения компании, версия ${company.revision}.`
            : 'Сведения компании ещё не настроены. ИИ использует состав и ваше задание.'}</p>}
      {usingProfile && profileStatus === 'ready' && company?.data.issuer_details && <details className="company-requisites-confirmed">
        <summary>Реквизиты отправителя для PDF</summary>
        <pre>{formatPartyDetails(company.data.issuer_details)}</pre>
        <p>Сохраняются в версии предложения. ИНН, КПП, ОГРН и адрес не передаются генератору текста.</p>
      </details>}
      <button type="button" className="text-button" disabled={busy || profileStatus === 'loading'}
        onClick={() => { setWithoutSavedProfile(false); setProfileRetry(value => value + 1); }}>Обновить сведения компании</button>
      {profileStatus === 'error' && <button type="button" className="button button-white"
        onClick={() => setWithoutSavedProfile(true)}>Продолжить без сохранённых сведений</button>}
    </div>}
    {profileStatus === 'ready' && <div className="company-logo-preview">
      <img src={usingProfile && company?.data.logo ? `data:image/png;base64,${company.data.logo.data_base64}`
        : '/assets/kpdesk-default-v1.png'} alt={usingProfile && company?.data.logo ? 'Логотип отправителя' : 'Логотип KPDesk по умолчанию'}/>
      <p>{usingProfile && company?.data.logo ? 'В PDF будет использован ваш логотип.'
        : 'Свой логотип не загружен — в PDF будет логотип KPDesk.'}</p>
    </div>}
    <label>От кого предложение
      <input value={context.issuer_name} maxLength={200} disabled={busy}
        onChange={event => changeContext('issuer_name', event.target.value)}/>
    </label>
    <label>Для кого предложение
      <input value={context.recipient_name} maxLength={200} disabled={busy}
        placeholder="Клиент или организация — если известны"
        onChange={event => changeContext('recipient_name', event.target.value)}/>
    </label>
    <details className="company-recipient-lookup">
      <summary>Заполнить заказчика по ИНН</summary>
      <CompanyInnLookup organizationId={organizationId} csrf={csrf} disabled={busy}
        onApply={party => { changeContext('recipient_name', party.short_name); setRecipientDetails(party); }}/>
    </details>
    {recipientDetails && <div role="region" aria-label="Реквизиты заказчика" className="company-requisites-confirmed">
      <p>В КП будут добавлены реквизиты заказчика:</p><pre>{formatPartyDetails(recipientDetails)}</pre>
      <button type="button" className="text-button" disabled={busy} onClick={() => {
        setRecipientDetails(null); setReviewed(false); approvalKey.current = null;
      }}>Убрать реквизиты</button>
    </div>}
    <label>Задача клиента и пожелания к тексту
      <textarea value={context.manager_brief} maxLength={2000} rows={3} disabled={busy}
        onChange={event => changeContext('manager_brief', event.target.value)}/>
    </label>
    <label>Подтверждённые условия оплаты и поставки
      <textarea value={context.commercial_terms} maxLength={2000} rows={2} disabled={busy}
        placeholder="Только известные условия. Неуказанные условия будут вынесены на согласование."
        onChange={event => changeContext('commercial_terms', event.target.value)}/>
    </label>
    <label>Дата предложения
      <input type="date" value={issuedOn} disabled={busy} onChange={event => {
        setIssuedOn(event.target.value); setReviewed(false); approvalKey.current = null;
      }}/>
    </label>
    <QuoteDocumentAlternatives key={scope} organizationId={organizationId} quote={quote} csrf={csrf} disabled={busy}
      onChange={selected => {
        epoch.current++; setAlternatives(selected); setPreview(null); setReviewed(false); setConsent(false);
        setBusy(false); setError(''); approvalKey.current = null;
      }}/>
    <DocumentAppearanceSettings value={appearance} disabled={busy}
      hasCompanyFacts={Boolean(company?.data.about || company?.data.advantages)}
      onChange={value => { setAppearance(value); setReviewed(false); approvalKey.current = null; }}/>
    <label className="tenant-checkbox">
      <input type="checkbox" name="external-proposal-consent" checked={consent} disabled={busy} onChange={event => {
        setConsent(event.target.checked); setReviewed(false);
      }}/>
      Разрешаю передать задание, названия отправителя и заказчика, описание и преимущества компании и данные выбранных товаров в OpenRouter / NVIDIA Nemotron для подготовки и проверки текста КП
    </label>
    <p className="field-hint">Данные передаются внешнему сервису OpenRouter и поставщику модели NVIDIA.
      Перед отправкой проверьте состав сведений.</p>
    <p className="field-hint">Подготовка включает генерацию и проверку текста. Если найдены ошибки,
      сервис один раз исправит текст и проверит его повторно. Каждый запрос, включая повторные
      попытки и исправления формата ответа, учитывается в лимите. Итоговый текст нужно проверить
      перед утверждением.</p>
    <button type="button" className="button button-primary"
      disabled={!csrf || busy || profileStatus !== 'ready' || !consent || !context.issuer_name.trim() || !context.manager_brief.trim()}
      onClick={() => void generate()}>{busy ? 'Подготовка…' : 'Сгенерировать текст КП'}</button>
    {preview && <div role="group" aria-label="Текст предложения от ИИ">
      {(['headline', 'opening', 'solution', 'closing'] as const).map(field => <label key={field}>
        {{ headline: 'Заголовок предложения', opening: 'Вступление', solution: 'Описание товаров', closing: 'Завершение' }[field]}
        <textarea value={preview.narrative[field]} disabled={busy}
          rows={field === 'solution' ? 5 : 2}
          maxLength={{ headline: 160, opening: 1500, solution: 4000, closing: 1000 }[field]}
          onChange={event => {
            setPreview({ ...preview, narrative: { ...preview.narrative, [field]: event.target.value } });
            setReviewed(false); approvalKey.current = null;
          }}/>
      </label>)}
      {!!preview.alternatives?.length && <div className="composition-grid" role="group" aria-label="Варианты для утверждения">
        {preview.alternatives.map((alternative, index) => <article className="composition-card" key={index}>
          <DocumentAlternativeSummary alternative={alternative} title={`Вариант ${index + 2}`}/>
        </article>)}
      </div>}
      <section className="proposal-pdf-preview" aria-label="Предпросмотр клиентского КП">
        <h4>Проверьте PDF перед утверждением</h4>
        <p>Откройте документ и проверьте его содержимое. После изменения текста, условий или оформления обновите предпросмотр.</p>
        <button type="button" className="button button-white" disabled={busy || !csrf || !issuedOn}
          onClick={() => void showPdf()}>{freshPdf ? 'Обновить предпросмотр PDF' : 'Посмотреть КП перед утверждением'}</button>
        {freshPdf && pdfPreview && <>
          <iframe title="Предпросмотр коммерческого предложения" src={`${pdfPreview.url}#view=FitH&navpanes=0`}/>
          <a href={`${pdfPreview.url}#view=FitH&navpanes=0`} target="_blank" rel="noreferrer">Открыть PDF отдельно</a>
          <p>Предпросмотр не утверждён. Проверьте товары, суммы, условия и реквизиты.</p>
        </>}
        {!context.commercial_terms.trim() && <p role="status">Условия оплаты и поставки ещё не указаны.
          Дополните их или оставьте на согласование с клиентом.</p>}
      </section>
      <p>Проверьте факты и формулировки, основной и дополнительные составы. Редактирование текста необязательно.</p>
      <label className="tenant-checkbox">
        <input type="checkbox" checked={reviewed} disabled={busy || !freshPdf}
          onChange={event => setReviewed(event.target.checked)}/>
        Я проверил текст, состав, суммы и условия коммерческого предложения в предпросмотре
      </label>
      <button type="button" className="button button-primary"
        disabled={!csrf || busy || !freshPdf || !reviewed || !issuedOn}
        onClick={() => void approve()}>Утвердить КП и подготовить PDF</button>
    </div>}
  </section>;
}
