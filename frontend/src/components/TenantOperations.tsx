import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { FileDown, Plus, RotateCcw, Users } from 'lucide-react';
import { sessionApi, SessionApiError } from '../tenant-session';
import { categories, categoryLabel } from '../categories';
import { money, readableReason, REASONS } from '../domain';
import type { ConfirmedInstructions, InstructionProposal } from '../domain';
import { splitRequestForManualReview } from '../manual-request-split';
import { SupplierCatalogChanges } from './SupplierCatalogChanges';
import { SupplierPrecisionReview } from './SupplierPrecisionReview';
import { QuoteProposalComposer } from './QuoteProposalComposer';
import { QuoteTrainingFeedback } from './QuoteTrainingFeedback';
import { QuoteSharePanel } from './QuoteSharePanel';
import '../styles/proposal-share.css';
import { CompanyProfileSettings } from './CompanyProfileSettings';
import { CompositionVariants } from './CompositionVariants';
import { RequestAnalysisPanel, RequirementMatrix } from './RequestAnalysis';
import { SupplierQuoteComparison } from './SupplierQuoteComparison';
import { UniversalWorkspace } from './UniversalWorkspace';
import { TenantCandidateReview } from './TenantCandidateReview';
import type {
  ActiveTenantCatalog, QuoteLineInput, QuoteListItem, TenantExtractionPreview,
  QuoteReviewAudit, ReviewEvidence, TenantInvitation,
  TenantMember, TenantOrganization, TenantQuote,
  TenantCategorySuggestion, TenantMatchResponse, ManagerSkuSelection,
  ManagerDecisionHistory,
  RequestAnalysis,
  ProposalTemplateSeed,
} from '../tenant-session';

type Props = {
  organization: TenantOrganization;
  catalog: ActiveTenantCatalog | null;
  csrf: string | null;
  onOrganizationChange: () => void;
  onCatalogRefresh?: () => Promise<ActiveTenantCatalog>;
  onCanonicalReady?: (file: File) => void;
  compactNavigation?: boolean;
};
type FormInput = {
  description: string; quantity: number; unit: 'штука' | 'пачка' | 'коробка' | '';
  category: string;
  paper_format: string; paper_gsm: number;
  source_quote: string; brand: string;
  pen_ink_color: string; pen_ink_type: string;
  attributes: Record<string, string | number | string[] | null>;
};
const initialLine: FormInput = {
  description: '', quantity: 0, unit: '',
  category: 'unknown', paper_format: '', paper_gsm: 0,
  source_quote: '', brand: '', pen_ink_color: '', pen_ink_type: '',
  attributes: {},
};
const CATEGORIES = categories.map(item => item.id);

const restoredInput = (line: QuoteLineInput): FormInput => ({
  source_quote: line.source_quote,
  description: line.description,
  category: line.category ?? 'unknown',
  quantity: line.quantity ?? 0,
  unit: line.unit === 'штука' || line.unit === 'пачка' || line.unit === 'коробка'
    ? line.unit : '',
  brand: line.brand ?? '',
  paper_format: line.paper_format ?? '',
  paper_gsm: line.paper_gsm ?? 0,
  pen_ink_color: line.pen_ink_color ?? '',
  pen_ink_type: line.pen_ink_type ?? '',
  attributes: { ...line.attributes },
});

const readableError = (error: unknown) =>
  error instanceof Error ? error.message : 'Не удалось выполнить действие. Повторите попытку.';
const memberRoleLabel = (role: string) => ({ owner: 'Руководитель', editor: 'Менеджер', viewer: 'Только просмотр' })[role] ?? 'Участник';

const toLine = (line: FormInput): QuoteLineInput => ({
  source_quote: line.source_quote || line.description.trim(),
  description: line.description.trim(),
  category: line.category === 'unknown' ? null : line.category,
  quantity: line.quantity, unit: line.unit || null,
  brand: line.brand.trim() || null,
  paper_format: line.category === 'paper' ? line.paper_format.trim() || null : null,
  paper_gsm: line.category === 'paper' ? line.paper_gsm : null,
  pen_ink_color: line.category === 'pen' ? line.pen_ink_color.trim() || null : null,
  pen_ink_type: line.category === 'pen' ? line.pen_ink_type.trim() || null : null,
  attributes: line.attributes,
});

type EditorRow = { id: number; input: FormInput; llm: boolean; reviewed: boolean };
type LineML = {
  fingerprint: string;
  suggestion?: TenantCategorySuggestion;
  preview?: TenantMatchResponse;
  selectedSku?: string;
  approvedSaleQuantity?: number | null;
};
const lineFingerprint = (input: FormInput, catalogId: string | undefined) =>
  JSON.stringify([catalogId, toLine(input)]);

const analysisLine = (input: FormInput): QuoteLineInput => ({
  ...toLine(input),
  quantity: Number.isSafeInteger(input.quantity) && input.quantity > 0 && input.quantity <= 100000
    ? input.quantity : null,
  paper_gsm: input.category === 'paper' && Number.isSafeInteger(input.paper_gsm)
    && input.paper_gsm > 0 && input.paper_gsm <= 1000 ? input.paper_gsm : null,
});
const analysisIdentity = (lines: QuoteLineInput[]) => JSON.stringify(lines.map(line => [
  line.source_quote, line.description, line.category, line.quantity, line.unit, line.brand,
  line.paper_format, line.paper_gsm, line.pen_ink_color, line.pen_ink_type,
  Object.entries(line.attributes).sort(([a], [b]) => a.localeCompare(b)),
]));

function matchingItem(input: FormInput): object {
  const line = toLine(input);
  return {
    description: line.description, category: line.category,
    quantity: line.quantity, unit: line.unit, brand: line.brand,
    paper_format: line.paper_format, paper_gsm: line.paper_gsm,
    pen_ink_color: line.pen_ink_color, pen_ink_type: line.pen_ink_type,
    attributes: Object.fromEntries(
      Object.entries(line.attributes).filter(([, value]) => value !== null),
    ),
  };
}

export function TenantOperations({
  organization, catalog, csrf, onOrganizationChange, onCatalogRefresh, onCanonicalReady, compactNavigation = false,
}: Props) {
  const [invitations, setInvitations] = useState<TenantInvitation[]>([]);
  const [universalOpen, setUniversalOpen] = useState(false);
  const [members, setMembers] = useState<TenantMember[]>([]);
  const [quotes, setQuotes] = useState<QuoteListItem[]>([]);
  const [quotesCursor, setQuotesCursor] = useState<string | null>(null);
  const [quotesLoading, setQuotesLoading] = useState(false);
  const [quotesError, setQuotesError] = useState('');
  const quoteListVersion = useRef(0);
  const quoteListPending = useRef(false);
  const [quote, setQuote] = useState<TenantQuote | null>(null);
  const [editingApproved, setEditingApproved] = useState(false);
  const [historicalQuote, setHistoricalQuote] = useState<TenantQuote | null>(null);
  const [historyRevision, setHistoryRevision] = useState(1);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [editorRows, setEditorRows] = useState<EditorRow[]>([
    { id: 0, input: { ...initialLine }, llm: false, reviewed: true },
  ]);
  const nextRowId = useRef(1);
  const quoteReadVersion = useRef(0);
  // Unconfirmed writes keep their keys for an explicit retry within this editor.
  // No content/keys are persisted to browser storage and no automatic retry runs.
  const pendingQuoteWrites = useRef(new Map<string, string>());
  const historyReadVersion = useRef(0);
  // A request may finish after a row edit, another ML request or context switch.
  // Its output is only valid for the exact editor and tenant context that sent it.
  const editorEpoch = useRef(0);
  const mlRequestSequence = useRef(0);
  const contextRef = useRef(JSON.stringify([organization.id, organization.role, catalog?.version_id ?? null, csrf]));
  contextRef.current = JSON.stringify([organization.id, organization.role, catalog?.version_id ?? null, csrf]);
  const tenantRef = useRef(JSON.stringify([organization.id, organization.role, csrf]));
  tenantRef.current = JSON.stringify([organization.id, organization.role, csrf]);
  const lastCatalogVersion = useRef(catalog?.version_id ?? null);
  const [catalogBlocked, setCatalogBlocked] = useState(false);
  const [lineMl, setLineMl] = useState<Record<number, LineML>>({});
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<'viewer' | 'editor'>('viewer');
  const [inviteNew, setInviteNew] = useState(false);
  const [sourceText, setSourceText] = useState('');
  const [rulesText, setRulesText] = useState('');
  const [rulesProposal, setRulesProposal] = useState<InstructionProposal | null>(null);
  const [confirmedRules, setConfirmedRules] = useState<ConfirmedInstructions | null>(null);
  // Reject a response if the instructions or tenant context changed while it was in flight.
  const rulesPreviewVersion = useRef(0);
  const [externalConsent, setExternalConsent] = useState(false);
  const [previewSource, setPreviewSource] = useState('');
  const [reviewPreview, setReviewPreview] = useState<TenantExtractionPreview | null>(null);
  const [requestAnalysis, setRequestAnalysis] = useState<{ identity: string; report: RequestAnalysis } | null>(null);
  const [reviewAudit, setReviewAudit] = useState<QuoteReviewAudit | null>(null);
  const [managerAudit, setManagerAudit] = useState<ManagerDecisionHistory | null>(null);
  const [notice, setNotice] = useState('');
  const [companyProfileReload, setCompanyProfileReload] = useState(0);
  const [documentTemplateSeed, setDocumentTemplateSeed] = useState<ProposalTemplateSeed | null>(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const saveEditorIdentity = JSON.stringify([
    editorRows, lineMl, rulesText, confirmedRules, reviewPreview, previewSource,
  ]);
  const saveEditorIdentityRef = useRef(saveEditorIdentity);
  saveEditorIdentityRef.current = saveEditorIdentity;
  const isOwner = organization.role === 'owner';
  const canWrite = isOwner || organization.role === 'editor';
  const approvedReadOnly = Boolean(quote?.approved && !editingApproved);
  const analysisLines = editorRows.map(row => analysisLine(row.input));
  const currentAnalysisIdentity = JSON.stringify([contextRef.current, analysisIdentity(analysisLines)]);
  const visibleAnalysis = requestAnalysis?.identity === currentAnalysisIdentity ? requestAnalysis.report
    : reviewPreview?.analysis && analysisIdentity(reviewPreview.extracted_request.lines
      .map(line => analysisLine(restoredInput(line)))) === analysisIdentity(analysisLines)
      ? reviewPreview.analysis : null;
  const validEditorRows = editorRows.length >= 1 && editorRows.length <= 20 &&
    editorRows.every(row => row.reviewed && row.input.category !== 'unknown'
      && CATEGORIES.includes(row.input.category)
      && Boolean(row.input.unit && row.input.description.trim())
      && Number.isSafeInteger(row.input.quantity)
      && row.input.quantity >= 1 && row.input.quantity <= 100000
      && (row.input.category !== 'paper'
        || (Number.isSafeInteger(row.input.paper_gsm)
          && row.input.paper_gsm >= 1 && row.input.paper_gsm <= 1000)));
  const variantEpoch = editorEpoch.current;
  const variantScope = contextRef.current;
  const variantRules = JSON.stringify([rulesText, confirmedRules]);
  const variantRulesRef = useRef(variantRules);
  variantRulesRef.current = variantRules;

  const load = useCallback(async () => {
    const scope = tenantRef.current;
    const version = ++quoteListVersion.current;
    quoteListPending.current = true;
    setQuotesLoading(true);
    setQuotesError('');
    const [pendingInvites, quoteList, team] = await Promise.allSettled([
      sessionApi.invitations(), sessionApi.listQuotes(organization.id),
      isOwner ? sessionApi.members(organization.id) : Promise.resolve([]),
    ]);
    if (scope !== tenantRef.current || version !== quoteListVersion.current) return;
    quoteListPending.current = false;
    setQuotesLoading(false);
    if (pendingInvites.status === 'fulfilled') setInvitations(pendingInvites.value);
    else setError(readableError(pendingInvites.reason));
    if (quoteList.status === 'fulfilled') {
      setQuotes(quoteList.value.items);
      setQuotesCursor(quoteList.value.next_cursor ?? null);
    } else setQuotesError(readableError(quoteList.reason));
    if (team.status === 'fulfilled') setMembers(team.value);
    else setError(readableError(team.reason));
  }, [organization.id, organization.role, csrf, isOwner]);

  async function loadMoreQuotes() {
    if (!quotesCursor || quoteListPending.current) return;
    const scope = tenantRef.current;
    const version = ++quoteListVersion.current;
    quoteListPending.current = true;
    setQuotesLoading(true);
    setQuotesError('');
    try {
      const page = await sessionApi.listQuotes(organization.id, quotesCursor);
      if (scope !== tenantRef.current || version !== quoteListVersion.current) return;
      setQuotes(current => {
        const seen = new Set(current.map(item => item.quote_id));
        return [...current, ...page.items.filter(item => !seen.has(item.quote_id))];
      });
      setQuotesCursor(page.next_cursor ?? null);
    } catch (failure) {
      if (scope === tenantRef.current && version === quoteListVersion.current) {
        setQuotesError(readableError(failure));
      }
    } finally {
      if (scope === tenantRef.current && version === quoteListVersion.current) {
        quoteListPending.current = false;
        setQuotesLoading(false);
      }
    }
  }

  useEffect(() => {
    pendingQuoteWrites.current.clear();
    setQuotes([]);
    setQuotesCursor(null);
    quoteReadVersion.current++;
    setDocumentTemplateSeed(null);
    historyReadVersion.current++;
    setHistoricalQuote(null);
    setHistoryRevision(1);
    setHistoryLoading(false);
    editorEpoch.current++;
    mlRequestSequence.current++;
    setQuote(null);
    setEditingApproved(false);
    setLineMl({});
    setEditorRows([{
      id: nextRowId.current++, input: { ...initialLine, attributes: {} },
      llm: false, reviewed: true,
    }]);
    setSourceText('');
    rulesPreviewVersion.current++;
    setRulesText('');
    setRulesProposal(null);
    setConfirmedRules(null);
    setPreviewSource('');
    setReviewPreview(null);
    setReviewAudit(null);
    setManagerAudit(null);
    setExternalConsent(false);
    setError('');
    setNotice('');
    setCatalogBlocked(false);
    setPending(false);
    void load();
    return () => { quoteListVersion.current++; };
  }, [load]);

  useLayoutEffect(() => {
    const version = catalog?.version_id ?? null;
    if (version === lastCatalogVersion.current) return;
    const previousCatalog = lastCatalogVersion.current;
    lastCatalogVersion.current = version;
    historyReadVersion.current++;
    setHistoricalQuote(null);
    setHistoryLoading(false);
    editorEpoch.current++;
    mlRequestSequence.current++;
    // Initial null → hydrated catalog must not silently cancel a persisted
    // quote read started during login. A genuine change of a known catalog
    // still invalidates in-flight quote reads and all manager SKU evidence.
    if (previousCatalog !== null) quoteReadVersion.current++;
    setPending(false);
    setLineMl({});
    setEditorRows(rows => rows.map(row =>
      row.input.description.trim() || row.input.source_quote.trim()
        ? { ...row, llm: true, reviewed: false } : row,
    ));
    setCatalogBlocked(false);
  }, [catalog?.version_id]);

  const action = async (callback: () => Promise<void>) => {
    const scope = contextRef.current;
    if (!csrf) {
      setError('Для изменения данных необходимо повторно войти в аккаунт.');
      return;
    }
    setPending(true);
    setError('');
    setNotice('');
    try { await callback(); }
    catch (err) {
      if (contextRef.current !== scope) return;
      if (err instanceof SessionApiError && err.status === 409
        && (err.code === 'stale_revision' || err.code === 'stale_catalog')) {
        if (err.code === 'stale_revision') {
          setError('КП уже изменено другим сотрудником. Ваша новая версия не сохранена. '
            + 'Откройте последнюю версию КП и проверьте товары перед повторным сохранением.');
        } else {
          setCatalogBlocked(true);
          setLineMl({});
          setEditorRows(rows => rows.map(row => ({ ...row, llm: true, reviewed: false })));
          setError('Каталог изменился. Ваша новая версия не сохранена. '
            + 'Обновите активный прайс и повторно проверьте товары, характеристики и цены.');
          if (onCatalogRefresh) {
            try {
              await onCatalogRefresh();
            } catch {
              setError('Каталог изменился, но обновить активный прайс не удалось. '
                + 'Повторите загрузку прайса, затем проверьте все позиции.');
            }
          }
        }
      } else {
        setError(readableError(err));
      }
    }
    finally {
      if (contextRef.current === scope) setPending(false);
    }
  };

  const invite = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void action(async () => {
      if (inviteNew) {
        await sessionApi.inviteByEmail(
          organization.id, inviteEmail.trim().toLowerCase(), inviteRole, csrf!,
        );
        setNotice('Если адрес доступен для приглашения, письмо будет отправлено.');
      } else {
        await sessionApi.invite(
          organization.id, inviteEmail.trim().toLowerCase(), inviteRole, csrf!,
        );
        setNotice('Приглашение создано. Сотрудник увидит его после входа.');
      }
      setInviteEmail('');
      await load();
    });
  };

  const accept = (invitationId: string) => {
    void action(async () => {
      await sessionApi.acceptInvite(invitationId, csrf!);
      setNotice('Приглашение принято.');
      onOrganizationChange();
      await load();
    });
  };

  const change = (member: TenantMember, role: 'viewer' | 'editor') => {
    void action(async () => {
      await sessionApi.changeRole(organization.id, member.user_id, role, csrf!);
      setNotice('Роль сотрудника изменена.');
      await load();
    });
  };

  const revoke = (member: TenantMember) => {
    void action(async () => {
      await sessionApi.revokeMember(organization.id, member.user_id, csrf!);
      setNotice('Доступ сотрудника отозван.');
      await load();
    });
  };

  const updateLine = (id: number, changes: Partial<FormInput>) => {
    editorEpoch.current++;
    mlRequestSequence.current++;
    setLineMl(current => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    setEditorRows(rows => rows.map(row =>
      row.id === id
        ? { ...row, input: { ...row.input, ...changes }, reviewed: !row.llm }
        : row,
    ));
  };
  const addLine = () => {
    editorEpoch.current++;
    mlRequestSequence.current++;
    setEditorRows(rows => rows.length >= 20 ? rows : [
      ...rows, {
        id: nextRowId.current++, input: { ...initialLine },
        llm: false, reviewed: true,
      },
    ]);
  };
  const removeLine = (id: number) => {
    editorEpoch.current++;
    mlRequestSequence.current++;
    setLineMl(current => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    setEditorRows(rows => rows.length > 1 ? rows.filter(row => row.id !== id) : rows);
  };

  const suggestCategory = (row: EditorRow) => {
    if (!csrf || !canWrite || !row.input.description.trim() || catalogBlocked) return;
    const epoch = editorEpoch.current;
    const sequence = ++mlRequestSequence.current;
    const scope = contextRef.current;
    const fingerprint = lineFingerprint(row.input, catalog?.version_id);
    void action(async () => {
      const suggestion = await sessionApi.suggestCategory(
        organization.id, row.input.description.trim(),
        row.input.category === 'unknown' ? null : row.input.category, csrf,
      );
      if (editorEpoch.current !== epoch || mlRequestSequence.current !== sequence
        || contextRef.current !== scope) return;
      setLineMl(current => ({
        ...current,
        [row.id]: { fingerprint, suggestion },
      }));
    });
  };
  const previewMatches = (row: EditorRow, mode: 'ml' | 'rapidfuzz' | 'tfidf' = 'ml') => {
    if (rulesText.trim() && (!confirmedRules || confirmedRules.raw_text !== rulesText)) {
      setError('Сначала проверьте и подтвердите условия подбора.');
      return;
    }
    if (catalogBlocked) {
      setError('Прайс изменился. Обновите его и подберите товары заново.');
      return;
    }
    if (!csrf || !catalog || !canWrite || !CATEGORIES.includes(row.input.category)) {
      setError('Выберите категорию и загрузите прайс.');
      return;
    }
    const epoch = editorEpoch.current;
    const sequence = ++mlRequestSequence.current;
    const scope = contextRef.current;
    const fingerprint = lineFingerprint(row.input, catalog.version_id);
    void action(async () => {
      const preview = await sessionApi.previewMatches(
        organization.id, catalog.version_id, matchingItem(row.input), csrf,
        confirmedRules ?? undefined, mode,
      );
      if (editorEpoch.current !== epoch || mlRequestSequence.current !== sequence
        || contextRef.current !== scope) return;
      if (preview.mode !== mode) {
        setLineMl({});
        setError('Получен результат другого способа поиска. Повторите подбор товаров.');
        return;
      }
      if (preview.catalog_version_id !== catalog.version_id) {
        editorEpoch.current++;
        mlRequestSequence.current++;
        setCatalogBlocked(true);
        setLineMl({});
        setEditorRows(rows => rows.map(item => ({
          ...item, llm: true, reviewed: false,
        })));
        setError('Найденные товары относятся к другому прайсу. Обновите прайс и повторите подбор.');
        return;
      }
      setLineMl(current => ({
        ...current,
        [row.id]: { fingerprint, preview },
      }));
    });
  };
  const previewWholeRequest = () => {
    if (rulesText.trim() && (!confirmedRules || confirmedRules.raw_text !== rulesText)) {
      setError('Сначала проверьте и подтвердите условия подбора.');
      return;
    }
    if (!csrf || !catalog || !canWrite || pending || catalogBlocked || !validEditorRows) {
      setError('Перед подбором проверьте все товары из заявки и обновите прайс.');
      return;
    }
    const epoch = editorEpoch.current;
    const sequence = ++mlRequestSequence.current;
    const scope = contextRef.current;
    const rows = editorRows;
    void action(async () => {
      const result = await sessionApi.previewBatchMatches(
        organization.id, catalog.version_id, rows.map(row => matchingItem(row.input)),
        csrf, confirmedRules ?? undefined,
      );
      if (editorEpoch.current !== epoch || mlRequestSequence.current !== sequence
        || contextRef.current !== scope) return;
      if (result.catalog_version_id !== catalog.version_id) {
        throw new SessionApiError('stale_catalog', 'Каталог изменился.', 409);
      }
      if (result.mode !== 'tfidf' || result.lines.length !== rows.length
        || result.lines.some((line, index) => line.line_number !== index + 1
          || line.mode !== result.mode || line.catalog_version_id !== catalog.version_id
          || !line.requires_manager_confirmation)) {
        setLineMl({});
        throw new Error('Ответ не соответствует заявке. Повторите подбор товаров.');
      }
      setLineMl(Object.fromEntries(rows.map((row, index) => [row.id, {
        fingerprint: lineFingerprint(row.input, catalog.version_id),
        preview: result.lines[index]!,
      }])));
      const empty = result.lines.filter(line => line.candidates.length === 0).length;
      setNotice(`Подбор завершён для ${rows.length} позиций. Проверьте предложенные товары и выберите подходящие.`
        + (empty ? ` Не найдено товаров для позиций: ${empty}. Уточните требования или прайс.` : ''));
    });
  };
  const checkRequestCompleteness = () => {
    if (!csrf || !canWrite || pending) return;
    const epoch = editorEpoch.current;
    const identity = currentAnalysisIdentity;
    void action(async () => {
      const report = await sessionApi.analyzeRequest(organization.id, analysisLines, csrf);
      if (epoch !== editorEpoch.current) return;
      setRequestAnalysis({ identity, report });
    });
  };
  const chooseSku = (row: EditorRow, sku: string) => {
    const fingerprint = lineFingerprint(row.input, catalog?.version_id);
    setLineMl(current => {
      const existing = current[row.id];
      if (!existing || existing.fingerprint !== fingerprint
        || (sku && !existing.preview?.candidates.some(candidate =>
          candidate.sku === sku && candidate.status === 'compatible'))) {
        return current;
      }
      return {
        ...current,
        [row.id]: {
          ...existing, selectedSku: sku || undefined, approvedSaleQuantity: null,
        },
      };
    });
  };

  const setSaleQuantity = (row: EditorRow, raw: string) => {
    setLineMl(current => {
      const previous = current[row.id];
      if (!previous || previous.fingerprint !== lineFingerprint(row.input, catalog?.version_id)
        || !previous.selectedSku) return current;
      return {
        ...current,
        [row.id]: {
          ...previous,
          approvedSaleQuantity: raw ? Number(raw) : null,
        },
      };
    });
  };

  const useManualSplit = (reason: string) => {
    quoteReadVersion.current++;
    editorEpoch.current++;
    mlRequestSequence.current++;
    try {
      const lines = splitRequestForManualReview(sourceText);
      pendingQuoteWrites.current.clear();
      setQuote(null);
      setLineMl({});
      setManagerAudit(null);
      setReviewAudit(null);
      setReviewPreview(null);
      setPreviewSource(sourceText);
      setEditorRows(lines.map(description => ({
        id: nextRowId.current++,
        input: {
          ...initialLine, source_quote: description, description,
          category: 'unknown', quantity: 0, unit: '',
          paper_format: '', paper_gsm: 0, attributes: {},
        },
        llm: true, reviewed: false,
      })));
      setExternalConsent(false);
      setError('');
      setNotice(reason + ' В строках сохранён только исходный текст. Заполните категорию, '
        + 'количество, единицу и характеристики, затем подтвердите каждую позицию. '
        + 'Без ручной проверки КП не сохраняется.');
    } catch (failure) {
      setError(readableError(failure));
    }
  };

  const extractText = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!csrf || !sourceText.trim() || !externalConsent || !canWrite) {
      setError('Войдите в аккаунт, добавьте текст заявки и подтвердите согласие на её передачу в OpenRouter.');
      return;
    }
    const scope = contextRef.current;
    const source = sourceText;
    const epoch = ++editorEpoch.current;
    mlRequestSequence.current++;
    quoteReadVersion.current++;
    setPending(true);
    setError('');
    setNotice('');
    try {
      const preview = await sessionApi.extractRequest(
        organization.id, source, csrf, externalConsent,
      );
      if (contextRef.current !== scope || editorEpoch.current !== epoch) return;
      const lines = preview.extracted_request.lines;
      if (!lines.length || lines.length > 20 || !preview.requires_human_review
        || preview.approved) {
        throw new Error('Невозможно использовать пустой или неподтверждённый ответ.');
      }
      if (lines.some(line => !sourceText.includes(line.source_quote))) {
        throw new Error('Распознанная позиция не найдена в исходном тексте. Проверьте заявку.');
      }
      pendingQuoteWrites.current.clear();
      setQuote(null);
      setLineMl({});
      setReviewAudit(null);
      setReviewPreview(preview);
      if (preview.instruction_proposal) {
        rulesPreviewVersion.current++;
        const previous = rulesText.trim();
        const extractedRules = preview.instruction_proposal;
        setRulesText(previous && previous !== extractedRules.raw_text
          ? previous + '\n' + extractedRules.raw_text : extractedRules.raw_text);
        setRulesProposal(previous && previous !== extractedRules.raw_text ? null : extractedRules);
        setConfirmedRules(null);
      }
      setPreviewSource(sourceText);
      setEditorRows(lines.map(line => ({
        id: nextRowId.current++,
        input: {
          description: line.description,
          source_quote: line.source_quote,
          category: line.category ?? 'unknown',
          quantity: line.quantity ?? 0,
          unit: line.unit === 'штука' || line.unit === 'пачка'
            || line.unit === 'коробка' ? line.unit : '',
          brand: line.brand ?? '',
          paper_format: line.paper_format ?? '',
          paper_gsm: line.paper_gsm ?? 0,
          pen_ink_color: line.pen_ink_color ?? '',
          pen_ink_type: line.pen_ink_type ?? '',
          attributes: line.attributes,
        },
        llm: true, reviewed: false,
      })));
      setExternalConsent(false);
      setNotice((preview.cache_hit
        ? 'Использован сохранённый результат распознавания: '
        : 'Распознано ') + lines.length
        + ' позиций. Проверьте исходные цитаты, категории, количество и параметры. '
        + 'До подтверждения каждой строки КП не сохраняется.');
    } catch (error) {
      if (contextRef.current !== scope || editorEpoch.current !== epoch) return;
      if (error instanceof SessionApiError && [429, 502, 503, 504].includes(error.status)) {
        useManualSplit('Текст заявки разбит на строки для ручного заполнения.');
        const message = readableError(error);
        setError(current => current ? message + ' ' + current : message);
      } else {
        setError(readableError(error));
      }
    } finally {
      if (contextRef.current === scope) setPending(false);
    }
  };

  const previewRules = () => {
    if (!rulesText.trim() || !csrf) return;
    const submittedText = rulesText;
    const version = ++rulesPreviewVersion.current;
    const scope = contextRef.current;
    void action(async () => {
      const preview = await sessionApi.previewQuoteInstructions(
        organization.id, submittedText, csrf,
      );
      if (version !== rulesPreviewVersion.current || scope !== contextRef.current
        || preview.raw_text !== submittedText) return;
      setRulesProposal(preview);
      setConfirmedRules(null);
      if (!preview.can_confirm) {
        setError('Часть условий не распознана. Уточните их и повторите проверку.');
      }
    });
  };

  const submitQuote = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canWrite || !csrf || pending) return;
    const scope = contextRef.current;
    if (rulesText.trim() && (!confirmedRules || confirmedRules.raw_text !== rulesText)) {
      setError('Сначала проверьте и подтвердите условия подбора и расчёта.');
      return;
    }
    if (catalogBlocked) {
      setError('Прайс устарел. Сначала обновите каталог и перепроверьте позиции.');
      return;
    }
    if (!catalog) { setError('Сначала загрузите и опубликуйте прайс.'); return; }
    if (!validEditorRows) {
      setError('Проверьте название, категорию, количество и единицу у каждой позиции. В КП может быть до 20 позиций.');
      return;
    }
    const version = ++quoteReadVersion.current;
    const submittedEditor = saveEditorIdentityRef.current;
    const current = () => contextRef.current === scope && quoteReadVersion.current === version;
    void action(async () => {
      try {
        const lines = editorRows.map(row => toLine(row.input));
        const review: ReviewEvidence | undefined = reviewPreview ? {
          extraction_id: reviewPreview.extraction_id,
          source_text: previewSource,
          original_extracted_request: reviewPreview.extracted_request,
          reviewed_lines: editorRows.map(row => row.reviewed),
        } : undefined;
        const selections: ManagerSkuSelection[] = editorRows.flatMap((row, index) => {
          const state = lineMl[row.id];
          if (!state?.selectedSku) return [];
          if (state.fingerprint !== lineFingerprint(row.input, catalog.version_id)
            || !state.preview?.candidates.some(candidate =>
              candidate.sku === state.selectedSku && candidate.status === 'compatible')) {
            throw new Error('Товары изменились. Повторите подбор для позиции.');
          }
          return [{
            line_number: index + 1,
            sku: state.selectedSku,
            approved_sale_quantity: state.approvedSaleQuantity ?? null,
          }];
        });
        const extra: [ReviewEvidence?, ManagerSkuSelection[]?, ConfirmedInstructions?] =
          confirmedRules
            ? [review, selections.length ? selections : undefined, confirmedRules]
            : selections.length ? [review, selections] : review ? [review] : [];
        const identity = JSON.stringify([
          scope, quote?.quote_id ?? null, quote?.revision ?? null, catalog.version_id, lines, extra,
        ]);
        let key = pendingQuoteWrites.current.get(identity);
        if (!key) {
          if (pendingQuoteWrites.current.size >= 32) {
            throw new Error('Не удалось подтвердить предыдущие сохранения. Проверьте список КП перед новой попыткой.');
          }
          key = crypto.randomUUID();
          pendingQuoteWrites.current.set(identity, key);
        }
        const saved = quote
          ? await sessionApi.reviseQuote(
              organization.id, quote.quote_id, quote.revision,
              catalog.version_id, lines, csrf!, key, ...extra,
            )
          : await sessionApi.createQuote(
              organization.id, catalog.version_id, lines, csrf!, key, ...extra,
            );
        if (!current()) return;
        if (!saved || Array.isArray(saved) || typeof saved.quote_id !== 'string' || !saved.quote_id
          || (quote && saved.quote_id !== quote.quote_id)
          || saved.revision !== (quote ? quote.revision + 1 : 1)
          || saved.catalog_version_id !== catalog.version_id || !Array.isArray(saved.lines)
          || typeof saved.snapshot_sha256 !== 'string' || !saved.snapshot_sha256
          || saved.approved !== false || typeof saved.ready_for_draft !== 'boolean') {
          throw new Error('Не удалось подтвердить сохранение товаров. Откройте КП из списка и проверьте его состав перед повторной попыткой.');
        }
        if (saveEditorIdentityRef.current !== submittedEditor) {
          setNotice('Отправленный состав сохранён. В редакторе остались более новые правки. '
            + 'Сохранённое КП можно открыть из списка.');
          await load();
          return;
        }
        if (pendingQuoteWrites.current.get(identity) === key) pendingQuoteWrites.current.delete(identity);
        historyReadVersion.current++;
        setHistoricalQuote(null);
        setHistoryRevision(1);
        setQuote(saved);
        setManagerAudit(null);
        if (saved.lines.some(line => line.status === 'manager_selected')) {
          try {
            const recorded = await sessionApi.managerDecisions(
              organization.id, saved.quote_id, saved.revision,
            );
            if (!current()) return;
            setManagerAudit(recorded);
          } catch (auditError) {
            if (!current()) return;
            setError('КП сохранено, но история выбора товаров недоступна: ' +
              readableError(auditError));
          }
        }
        setReviewPreview(null);
        if (review) {
          try {
            const audit = await sessionApi.quoteReview(
              organization.id, saved.quote_id, saved.revision,
            );
            if (!current()) return;
            setReviewAudit(audit);
          } catch (error) {
            if (!current()) return;
            setReviewAudit(null);
            setError('КП сохранено, но историю проверки открыть не удалось: ' +
              readableError(error));
          }
        } else {
          setReviewAudit(null);
        }
        setNotice(compactNavigation ? 'Состав сохранён. Подготовьте предложение клиенту' : 'КП сохранено. Версия ' + saved.revision
          + (review ? '. Результат вашей проверки сохранён.' :
            '. Проверка распознавания не сохранена.'));
        await load();
      } catch (failure) {
        // A save belongs to the editor that sent it, including its errors.
        if (current()) throw failure;
      }
    });
  };

  const readQuote = async (quoteId: string) => {
    const version = ++quoteReadVersion.current;
    rulesPreviewVersion.current++;
    historyReadVersion.current++;
    setHistoricalQuote(null);
    setHistoryRevision(1);
    setHistoryLoading(false);
    editorEpoch.current++;
    mlRequestSequence.current++;
    const scope = tenantRef.current;
    setError('');
    try {
      const found = await sessionApi.getQuote(organization.id, quoteId);
      if (version !== quoteReadVersion.current || tenantRef.current !== scope) return;
      pendingQuoteWrites.current.clear();
      setQuote(found);
      setEditingApproved(false);
      setDocumentTemplateSeed(null);
      setRulesText(found.instructions?.raw_text ?? '');
      setConfirmedRules(found.instructions ?? null);
      setRulesProposal(found.instructions ? {
        raw_text: found.instructions.raw_text,
        rules: found.instructions.rules,
        unresolved_requirements: [],
        can_confirm: true,
      } : null);
      setManagerAudit(null);
      setLineMl({});
      setReviewPreview(null);
      setPreviewSource('');
      setSourceText(found.source_request?.lines.map(line => line.source_quote).join('\n').slice(0, 12000) ?? '');
      setExternalConsent(false);
      setReviewAudit(null);
      setEditorRows(found.source_request?.lines.length
        ? found.source_request.lines.map(line => ({
          id: nextRowId.current++, input: restoredInput(line),
          llm: true, reviewed: false,
        }))
        : [{
          id: nextRowId.current++, input: { ...initialLine, attributes: {} },
          llm: false, reviewed: true,
        }]);
      setNotice(found.approved ? '' : found.source_request?.lines.length
        ? 'Заявка восстановлена из сохранённой версии. Проверьте каждую строку '
          + 'и заново выберите товары: предыдущие решения не переносятся автоматически.'
        : 'У этой предыдущей версии нет сохранённых исходных характеристик. '
          + 'Создайте новую версию вручную; данные старого КП не изменены.');
      if (found.lines.some(line => line.status === 'manager_selected')) {
        try {
          const decisions = await sessionApi.managerDecisions(
            organization.id, quoteId, found.revision,
          );
          if (version !== quoteReadVersion.current || tenantRef.current !== scope) return;
          setManagerAudit(decisions);
        } catch (auditError) {
          if (version !== quoteReadVersion.current || tenantRef.current !== scope) return;
          setError('История выбора товаров недоступна: ' + readableError(auditError));
        }
      }
      try {
        const audit = await sessionApi.quoteReview(
          organization.id, quoteId, found.revision,
        );
        if (version !== quoteReadVersion.current || tenantRef.current !== scope) return;
        setReviewAudit(audit);
      } catch (error) {
        if (version !== quoteReadVersion.current || tenantRef.current !== scope) return;
        if (error instanceof SessionApiError && error.status !== 404) {
          setError(readableError(error));
        }
        setReviewAudit(null);
      }
    } catch (error) {
      if (version === quoteReadVersion.current && tenantRef.current === scope) {
        setError(readableError(error));
      }
    }
  };

  const openHistoricalRevision = async () => {
    if (!quote || !Number.isSafeInteger(historyRevision)
      || historyRevision < 1 || historyRevision >= quote.revision) return;
    const requestedId = quote.quote_id;
    const requestedRevision = historyRevision;
    const scope = contextRef.current;
    const sequence = ++historyReadVersion.current;
    setHistoryLoading(true);
    setHistoricalQuote(null);
    setError('');
    try {
      const earlier = await sessionApi.getQuote(
        organization.id, requestedId, requestedRevision,
      );
      if (sequence !== historyReadVersion.current || scope !== contextRef.current) return;
      if (earlier.quote_id !== requestedId || earlier.revision !== requestedRevision) {
        throw new Error('Получена другая версия КП. Откройте нужную версию заново.');
      }
      setHistoricalQuote(earlier);
    } catch (error) {
      if (sequence === historyReadVersion.current && scope === contextRef.current) {
        setError(readableError(error));
      }
    } finally {
      if (sequence === historyReadVersion.current && scope === contextRef.current) {
        setHistoryLoading(false);
      }
    }
  };

  const startNewQuote = () => {
    pendingQuoteWrites.current.clear();
    quoteReadVersion.current++;
    historyReadVersion.current++;
    setHistoricalQuote(null);
    setHistoryRevision(1);
    setHistoryLoading(false);
    editorEpoch.current++;
    mlRequestSequence.current++;
    setQuote(null);
    setEditingApproved(false);
    setDocumentTemplateSeed(null);
    setEditorRows([{
      id: nextRowId.current++,
      input: { ...initialLine, attributes: {} },
      llm: false,
      reviewed: true,
    }]);
    setLineMl({});
    setReviewPreview(null);
    setReviewAudit(null);
    setManagerAudit(null);
    setSourceText('');
    rulesPreviewVersion.current++;
    setRulesText('');
    setRulesProposal(null);
    setConfirmedRules(null);
    setPreviewSource('');
    setExternalConsent(false);
    setError('');
    setNotice('');
  };

  const useQuoteAsTemplate = () => {
    if (!canWrite || pending || !quote?.source_request?.lines.length) return;
    const source = quote;
    startNewQuote();
    setDocumentTemplateSeed({ appearance: source.document?.appearance ?? null,
      commercial_terms: source.document?.commercial_terms ?? '' });
    setEditorRows(source.source_request!.lines.map(line => ({
      id: nextRowId.current++, input: restoredInput(line), llm: true, reviewed: false,
    })));
    setSourceText(source.source_request!.lines.map(line => line.source_quote).join('\n').slice(0, 12000));
    if (source.instructions) {
      setRulesText(source.instructions.raw_text);
      setRulesProposal({ raw_text: source.instructions.raw_text, rules: source.instructions.rules,
        can_confirm: true, unresolved_requirements: [] });
    }
    setNotice('Новый черновик подготовлен по сохранённому КП. Проверьте заявку и условия, '
      + 'подтвердите правила и подберите товары по актуальному прайсу. Оформление перенесено; '
      + 'текст предложения будет подготовлен заново.');
  };

  return <div className="tenant-operations">
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <div className="alert success" role="status">{notice}</div>}

    <details id="workspace-universal" className="workspace-disclosure"
      onToggle={event => setUniversalOpen(event.currentTarget.open)}>
      <summary>Прайс, заявка и КП</summary>
      {universalOpen && <UniversalWorkspace organizationId={organization.id}
        organizationName={organization.name} csrf={csrf} role={organization.role}/>}
    </details>

    {canWrite && !approvedReadOnly && <section id="workspace-request" className="access-catalog-state"
      aria-labelledby="request-extraction-title">
      <div className="workspace-section-heading"><div><span className="workspace-step">Шаг 2</span>
      <h2 id="request-extraction-title">Заявка клиента</h2></div></div>
      <p>Вставьте заявку клиента. ИИ выделит товары, количество и характеристики —
        проверьте их перед подбором. Текст передаётся внешнему сервису только с вашего
        согласия. Стоимость рассчитывается по загруженному прайсу.</p>
      <form className="access-form" onSubmit={event => void extractText(event)}>
        <label>Текст заявки (до 12 000 символов)
          <textarea rows={5} maxLength={12000} required
            value={sourceText} onChange={event => {
              editorEpoch.current++;
              setSourceText(event.target.value);
            }}
            placeholder="2 пачки бумаги А4 80 г/м²; 1 пачка бумаги А4 80 г/м²"/>
        </label>
        <label className="tenant-checkbox">
          <input type="checkbox" checked={externalConsent}
            onChange={event => setExternalConsent(event.target.checked)}/>
          Разрешаю передать текст заявки в OpenRouter / NVIDIA Nemotron для извлечения позиций
        </label>
        <button className="button button-primary" type="submit"
          disabled={!csrf || pending || !sourceText.trim() || !externalConsent}>
          {pending ? 'Распознаём…' : 'Распознать заявку'}
        </button>
        <button className="button button-white" type="button"
          disabled={!csrf || pending || !sourceText.trim()}
          onClick={() => useManualSplit('Строки подготовлены без передачи текста во внешний ИИ.')}>
          Разделить на позиции без ИИ
        </button>
      </form>
    </section>}

    {canWrite && !approvedReadOnly && <section id="workspace-quote-editor" className="access-catalog-state" aria-labelledby="quote-editor-title">
      <span className="workspace-step">Шаг 3</span>
      <h2 id="quote-editor-title">{quote ? 'Новая версия КП' : 'Создать КП'}</h2>
      {catalogBlocked && <div className="alert warning" role="status">
        <p>Прайс изменился. Предыдущий выбор товаров сброшен.
          Обновите прайс и заново подтвердите каждую позицию.</p>
        <button type="button" className="button button-white"
          disabled={!onCatalogRefresh || pending}
          onClick={() => {
            void onCatalogRefresh?.().catch(err => setError(readableError(err)));
          }}>
          Обновить активный прайс
        </button>
      </div>}
      <p>Проверьте позиции заявки и выберите товары из прайса. Если подходящего товара нет, позиция останется на проверке.</p>
      {previewSource && <details>
        <summary>Исходный текст заявки</summary>
        <pre className="tenant-original-request">{previewSource}</pre>
      </details>}
      {editorRows.some(row => row.llm && !row.reviewed) && <p role="status">
        Сверьте каждую позицию с исходной заявкой и подтвердите проверку.
      </p>}
      <form className="access-form" onSubmit={submitQuote}>
        <section className="tenant-quote-rules" aria-labelledby="tenant-quote-rules-heading">
          <h3 id="tenant-quote-rules-heading">Наценка, бюджет и бренды</h3>
          <p>Укажите наценку, максимальную сумму, нужный бренд или бренды для исключения.
            Проверьте и подтвердите условия перед расчётом. Если условие не распознано, уточните его.</p>
          <label>Условия подбора и расчёта
            <textarea value={rulesText} maxLength={2000} rows={3} disabled={pending}
              placeholder="Наценка 18%. Бюджет до 250000 рублей. Исключи бренд Acme."
              onChange={event => {
                rulesPreviewVersion.current++;
                editorEpoch.current++;
                mlRequestSequence.current++;
                setLineMl({});
                setRulesText(event.target.value);
                setRulesProposal(null);
                setConfirmedRules(null);
              }}/>
          </label>
          {rulesText.trim() && <button type="button" className="button button-white"
            disabled={!csrf || pending} onClick={previewRules}>Проверить условия</button>}
          {rulesProposal && <div role="status">
            <p>Наценка: {rulesProposal.rules.markup_percent}%.
              {rulesProposal.rules.budget_max_rub
                ? ' Бюджет: ' + money(rulesProposal.rules.budget_max_rub) + '.'
                : ' Бюджет не установлен.'}
              {rulesProposal.rules.required_brand
                ? ' Только бренд: ' + rulesProposal.rules.required_brand + '.' : ''}
              {rulesProposal.rules.excluded_brands?.length
                ? ' Исключены бренды: ' + rulesProposal.rules.excluded_brands.join(', ') + '.' : ''}</p>
            {rulesProposal.unresolved_requirements.length > 0 &&
              <p>Не удалось применить: {rulesProposal.unresolved_requirements
                .map(issue => Object.hasOwn(REASONS, issue) ? REASONS[issue] : issue).join('; ')}.</p>}
            {confirmedRules
              ? <p>Условия подтверждены для следующей версии КП.</p>
              : rulesProposal.can_confirm && <button type="button"
                className="button button-primary" disabled={pending}
                onClick={() => {
                  if (rulesProposal.raw_text !== rulesText) return;
                  editorEpoch.current++;
                  mlRequestSequence.current++;
                  setLineMl({});
                  setConfirmedRules({
                    raw_text: rulesText, rules: rulesProposal.rules,
                  });
                  setNotice('Условия подтверждены и будут учтены при расчёте стоимости.');
                }}>Подтвердить условия</button>}
          </div>}
        </section>
        <RequestAnalysisPanel key={currentAnalysisIdentity} analysis={visibleAnalysis}
          onRefresh={checkRequestCompleteness} busy={pending}
          canAnalyze={Boolean(csrf && analysisLines.length && analysisLines.every(line => line.description.trim()))}/>
        <div className="tenant-actions">
          <button type="button" className="button button-primary"
            disabled={!csrf || !catalog || pending || catalogBlocked || !validEditorRows}
            onClick={previewWholeRequest}>Подобрать товары по всей заявке</button>
          <p>Сервис предложит товары для всех позиций с учётом подтверждённых условий.
            Проверьте предложенный состав перед сохранением КП.</p>
        </div>
        <CompositionVariants organizationId={organization.id} catalogId={catalog?.version_id ?? ''}
          csrf={csrf ?? ''} identity={JSON.stringify([variantEpoch, variantScope, variantRules])}
          requestedItems={editorRows.map(row => matchingItem(row.input))}
          instructions={confirmedRules ?? undefined}
          enabled={Boolean(csrf && catalog && !pending && !catalogBlocked && validEditorRows
            && (!rulesText.trim() || confirmedRules?.raw_text === rulesText))}
          onApply={(result, variant) => {
            if (editorEpoch.current !== variantEpoch || contextRef.current !== variantScope
              || variantRulesRef.current !== variantRules || !catalog) return;
            mlRequestSequence.current++;
            setLineMl(Object.fromEntries(editorRows.map((row, index) => [row.id, {
              fingerprint: lineFingerprint(row.input, catalog.version_id),
              preview: result.lines[index]!, selectedSku: variant.lines[index]!.sku,
              approvedSaleQuantity: null,
            }])));
            setNotice('Товары добавлены в КП. Проверьте состав перед сохранением; товары и цены будут проверены повторно.');
          }}/>
        {editorRows.map((row, index) => {
          const candidateState = lineMl[row.id];
          const ml = candidateState?.fingerprint === lineFingerprint(row.input, catalog?.version_id)
            ? candidateState : undefined;
          const categorySuggestion = ml?.suggestion;
          const matchPreview = ml?.preview;
          return <fieldset key={row.id} className="tenant-quote-line">
          <legend>Позиция {index + 1}</legend>
          <label>Наименование позиции
            <input value={row.input.description} required maxLength={500}
              onChange={e => updateLine(row.id, { description: e.target.value })}/>
          </label>
          <label>Категория
            <select value={row.input.category}
              onChange={e => updateLine(row.id, {
                category: e.target.value,
                attributes: {},
              })}>
              <option value="unknown">Выберите категорию</option>
              {CATEGORIES.map(category =>
                <option key={category} value={category}>{categoryLabel(category)}</option>)}
            </select>
          </label>
          <label>Количество
            <input type="number" min={1} max={100000} required
              value={row.input.quantity}
              onChange={e => updateLine(row.id, { quantity: Number(e.target.value) })}/>
          </label>
          <label>Единица
            <select required value={row.input.unit}
              onChange={e => updateLine(row.id, {
                unit: e.target.value as FormInput['unit'],
              })}>
              <option value="">Выберите единицу</option>
              <option value="штука">Штука</option>
              <option value="пачка">Пачка</option>
              <option value="коробка">Коробка</option>
            </select>
          </label>
          <label>Бренд (если указан в заявке)
            <input maxLength={150} value={row.input.brand}
              onChange={e => updateLine(row.id, { brand: e.target.value })}/>
          </label>
          {categories.find(category => category.id === row.input.category)?.fields.map(field => {
            const value = row.input.attributes[field.key];
            const displayed = Array.isArray(value) ? value.join('; ') : String(value ?? '');
            return <label key={field.key}>{field.label}
              <input type={field.type === 'number' ? 'number' : 'text'}
                min={field.type === 'number' ? 1 : undefined}
                value={displayed} placeholder={field.placeholder ?? 'Не указано'}
                onChange={event => {
                  const text = event.target.value.trim();
                  const updated = field.type === 'number'
                    ? (Number.isSafeInteger(Number(text)) && Number(text) > 0
                      ? Number(text) : null)
                    : field.type === 'list'
                      ? (text ? [...new Set(text.split(/[;\n]/).map(item => item.trim()).filter(Boolean))] : null)
                      : text || null;
                  updateLine(row.id, {
                    attributes: { ...row.input.attributes, [field.key]: updated },
                  });
                }}/>
            </label>;
          })}
          {row.input.category === 'pen' && <>
            <label>Цвет чернил
              <input maxLength={100} value={row.input.pen_ink_color}
                onChange={e => updateLine(row.id, { pen_ink_color: e.target.value })}/>
            </label>
            <label>Тип чернил
              <input maxLength={100} value={row.input.pen_ink_type}
                onChange={e => updateLine(row.id, { pen_ink_type: e.target.value })}/>
            </label>
          </>}
          {row.input.category === 'paper' && <>
            <label>Формат
              <input value={row.input.paper_format} maxLength={30}
                onChange={e => updateLine(row.id, { paper_format: e.target.value })}/>
            </label>
            <label>Плотность бумаги, г/м²
              <input type="number" min={1} max={1000}
                value={row.input.paper_gsm}
                onChange={e => updateLine(row.id, {
                  paper_gsm: Number(e.target.value),
                })}/>
            </label>
          </>}
          <div className="tenant-ml-matching">
            <button type="button" className="button button-primary"
              disabled={!csrf || !catalog || catalogBlocked || pending || !CATEGORIES.includes(row.input.category)}
              onClick={() => previewMatches(row, 'tfidf')}>
              Подобрать товары
            </button>
            <details open={compactNavigation ? undefined : true}>
              <summary>Дополнительные инструменты подбора</summary>
          <div className="tenant-ml-category">
            <button type="button" className="button button-white"
              disabled={!csrf || pending || !row.input.description.trim()}
              onClick={() => suggestCategory(row)}>
              Проверить категорию
            </button>
            {categorySuggestion && <div role="status">
                {categorySuggestion.abstained
                  ? <p>Не удалось однозначно определить категорию. Выберите её вручную.</p>
                  : <p>Предлагаемая категория: {categoryLabel(categorySuggestion.suggested_category)}.
                    Оценка {Math.round(categorySuggestion.confidence * 100)}%.
                    Требуется подтверждение менеджера.</p>}
                {categorySuggestion.suggested_category
                  && categorySuggestion.suggested_category !== row.input.category &&
                  <button type="button" className="button button-white"
                    onClick={() => updateLine(row.id, {
                      category: categorySuggestion.suggested_category!,
                      attributes: {}, paper_format: '', paper_gsm: 0,
                      pen_ink_color: '', pen_ink_type: '',
                    })}>
                    Подтвердить категорию
                  </button>}
              </div>}
          </div>
            <button type="button" className="button button-white"
              disabled={!csrf || !catalog || catalogBlocked || pending || !CATEGORIES.includes(row.input.category)}
              onClick={() => previewMatches(row)}>
              Подбор по характеристикам
            </button>
            <button type="button" className="button button-white"
              disabled={!csrf || !catalog || catalogBlocked || pending || !CATEGORIES.includes(row.input.category)}
              onClick={() => previewMatches(row, 'rapidfuzz')}>
              Поиск по названию
            </button>
              {matchPreview && <>
                <ul>
                  {matchPreview.candidates.slice(0, 5).map(candidate =>
                    <li key={candidate.sku}>
                      {candidate.sku}: {matchPreview.mode === 'ml'
                        ? 'оценка подбора' : 'сходство названия'} {Math.round(candidate.score * 100)}%,
                      {candidate.status === 'compatible' ? ' соответствует проверенным требованиям' :
                        ' нужна проверка: ' + candidate.reason_codes.map(readableReason).join(', ')}
                      {candidate.status === 'needs_review' && Boolean(candidate.requirements?.length)
                        && <RequirementMatrix checks={candidate.requirements}/>}
                    </li>)}
                </ul>
              </>}
            </details>
            {matchPreview && <div className="tenant-ml-results">
                <p>Способ подбора: {matchPreview.mode === 'ml'
                  ? 'по характеристикам' : matchPreview.mode === 'tfidf'
                    ? 'по тексту заявки' : 'по сходству названий'}. Проверьте характеристики и выберите товар.
                  При сохранении они проверяются повторно. Наличие и сроки поставки нужно уточнить отдельно.</p>
                {matchPreview.candidates.length === 0
                  ? <p role="status">Не удалось подобрать товары по указанным требованиям.
                    Проверьте категорию и характеристики или оставьте позицию на проверке.</p>
                  : <label>Выбранный товар
                    <select value={ml.selectedSku ?? ''}
                      onChange={event => chooseSku(row, event.target.value)}>
                      <option value="">Выберите товар после проверки</option>
                      {matchPreview.candidates.map(candidate =>
                        <option key={candidate.sku} value={candidate.sku}
                          disabled={candidate.status !== 'compatible'}>
                          {candidate.sku} — {candidate.name} —
                          {candidate.price_rub} ₽/{candidate.sale_unit}
                          {candidate.status === 'needs_review' ? ' (уточните характеристики)' : ''}
                        </option>)}
                    </select>
                  </label>}
                {ml.selectedSku && <label>
                  Подтверждённое количество единиц продажи (если требуется округление упаковки)
                  <input type="number" min={1} max={100000} step={1}
                    placeholder="Не требуется"
                    value={ml.approvedSaleQuantity ?? ''}
                    onChange={event => setSaleQuantity(row, event.target.value)}/>
                </label>}
                {ml.selectedSku && <RequirementMatrix checks={matchPreview.candidates
                  .find(candidate => candidate.sku === ml.selectedSku)?.requirements}/>}
              </div>}
          </div>
          {row.llm && <div className="tenant-extracted-review">
            <strong>Из заявки клиента:</strong>
            <blockquote>{row.input.source_quote}</blockquote>
            {Object.keys(row.input.attributes).length > 0 && <details>
              <summary>Распознанные характеристики</summary>
              <pre>{JSON.stringify(row.input.attributes, null, 2)}</pre>
              <button type="button" className="text-button"
                onClick={() => updateLine(row.id, { attributes: {} })}>
                Очистить непроверенные характеристики
              </button>
            </details>}
            <label className="tenant-checkbox">
              <input type="checkbox" checked={row.reviewed}
                onChange={event => setEditorRows(rows => rows.map(item =>
                  item.id === row.id
                    ? { ...item, reviewed: event.target.checked } : item,
                ))}/>
              Проверил позицию по исходной заявке
            </label>
          </div>}
          {editorRows.length > 1 && <button className="text-button danger"
            type="button" onClick={() => removeLine(row.id)}>
            Удалить позицию
          </button>}
        </fieldset>;
        })}
        <button className="button button-white" type="button"
          disabled={editorRows.length >= 20} onClick={addLine}>
          <Plus size={17} aria-hidden="true"/> Добавить позицию ({editorRows.length}/20)
        </button>
        <button className="button button-primary" type="submit"
          disabled={!csrf || !catalog || catalogBlocked || pending
            || editorRows.some(row => !row.reviewed)}>
          {compactNavigation ? 'Сохранить состав' : quote ? 'Создать следующую версию' : 'Рассчитать и сохранить КП'}
        </button>
      </form>
    </section>}

    <section id="workspace-quotes" className="access-catalog-state" aria-labelledby="quotes-title">
      <div className="tenant-section-header">
        <h2 id="quotes-title">Коммерческие предложения</h2>
        <button type="button" className="text-button" onClick={() => void load()}>
          <RotateCcw size={16} aria-hidden="true"/> Обновить
        </button>
      </div>
      {quotesError && <p role="alert">{quotesError}</p>}
      {quotes.length ? <ul className="tenant-list">
        {quotes.map(item => <li key={item.quote_id}>
          <span>КП {item.quote_id.slice(0, 8)} · версия {item.revision} ·
            {' '}{item.total_rub === null ? 'Требует проверки' : item.total_rub + ' ₽'}
            {' · '}{item.approved === true ? 'Утверждено'
              : item.approved === false ? 'Не утверждено' : 'Статус не загружен'}
          </span>
          <button type="button" className="button button-white"
            onClick={() => void readQuote(item.quote_id)}>Открыть</button>
        </li>)}
      </ul> : quotesLoading ? <p role="status">Загружаем КП…</p>
        : !quotesError && <p>В организации ещё нет сохранённых КП.</p>}
      {quotesCursor && <button type="button" className="button button-white"
        disabled={quotesLoading} onClick={() => void loadMoreQuotes()}>
        {quotesLoading ? 'Загружаем…' : 'Показать ещё'}
      </button>}
    </section>

    {quote && <section className="access-catalog-state" aria-label="Текущее коммерческое предложение">
      <h2>КП · версия {quote.revision}</h2>
      <details open={compactNavigation ? undefined : true}>
      <summary>Подробности расчёта</summary>
      {reviewAudit
        ? <p>Проверка менеджера: {new Date(
            reviewAudit.reviewed_at,
          ).toLocaleString('ru-RU')}. Пользователь: {
            reviewAudit.reviewer_user_id.slice(0, 8)
          }…</p>
        : <p>Проверка распознанной заявки для этой версии не сохранена.</p>}
      {quote.lines.some(line => line.status === 'manager_selected') &&
        <div className="tenant-manager-decisions">
          <h3>История выбора товаров</h3>
          {managerAudit && managerAudit.quote_id === quote.quote_id
            && managerAudit.revision === quote.revision
            ? <ul className="tenant-list">
              {quote.lines.filter(line => line.status === 'manager_selected').map(line => {
                const proof = managerAudit.items.find(item =>
                  item.line_number === line.line_number &&
                  item.sku === line.sku &&
                  item.catalog_version_id === quote.catalog_version_id);
                return <li key={line.line_number}>
                  {proof
                    ? <span>Строка {line.line_number}, артикул {proof.sku}.
                      Подтвердил пользователь {proof.approved_by_user_id.slice(0, 8)}…
                      {' '}({new Date(proof.recorded_at).toLocaleString('ru-RU')})</span>
                    : <span role="alert">Строка {line.line_number}: подтверждение
                        в истории не найдено.</span>}
                </li>;
              })}
            </ul>
            : <p role="status">Не удалось загрузить историю выбора товаров.
                Обновите КП и повторите попытку.</p>}
        </div>}
      {quote.instructions && <p>Условия расчёта этой версии:
        {' '}наценка {quote.instructions.rules.markup_percent}%,
        {' '}бюджет {money(quote.instructions.rules.budget_max_rub)}.
        {quote.instructions.rules.required_brand
          ? ' Только бренд: ' + quote.instructions.rules.required_brand + '.' : ''}
        {quote.instructions.rules.excluded_brands?.length
          ? ' Исключены: ' + quote.instructions.rules.excluded_brands.join(', ') + '.' : ''}</p>}
      {quote.base_total_rub !== undefined && quote.base_total_rub !== null &&
        <p>Сумма по прайсу: {money(quote.base_total_rub)}</p>}
      </details>
      <p>{quote.budget_exceeded ? 'Расчётная сумма КП: ' : 'Сумма КП: '}
        <strong>{money(quote.budget_exceeded
          ? quote.estimated_total_rub : quote.total_rub)}</strong></p>
      {quote.budget_exceeded && <p role="alert">Превышен подтверждённый бюджет.
        PDF заблокирован до пересмотра условий и сохранения новой версии.</p>}
      <ul className="tenant-list">
        {quote.lines.map(line => <li key={line.line_number}>
          <strong>{line.description}</strong> — {line.product_name ?? 'Требует подбора'} ·
          {' '}артикул: {line.sku ?? 'не выбран'} ·
          {line.product_brand && <> Бренд: {line.product_brand} · </>}
          {line.supplier_id && <> Поставщик: {line.supplier_id} ·
            версия прайса: {line.source_catalog_version_id} ·
            SHA-256: {line.source_sha256} · </>}
          {' '}{line.line_total_rub === null ? 'Нет цены' : line.line_total_rub + ' ₽'}
          {line.status === 'manager_selected' && <small> · товар выбран менеджером</small>}
          {line.status === 'needs_review' && <small> · Требует проверки</small>}
          {line.status === 'no_match' && <small> · Не удалось подобрать товар</small>}
          {line.reason_codes.length > 0 &&
            <small> · {line.reason_codes.map(readableReason).join('; ')}</small>}
          <RequirementMatrix checks={line.requirements}/>
        </li>)}
      </ul>
      <QuoteTrainingFeedback
        key={JSON.stringify([organization.id, quote.quote_id, quote.revision, quote.snapshot_sha256])}
        organizationId={organization.id} quote={quote} csrf={csrf} canManage={isOwner}/>
      <QuoteProposalComposer
        key={JSON.stringify([organization.id, quote.quote_id, quote.revision])}
        organizationId={organization.id} issuerName={organization.name} quote={quote}
        csrf={csrf} canWrite={canWrite}
        requestBrief={quote.source_request?.lines.map(line => line.source_quote).join('\n') ?? ''}
        useCompanyProfile={compactNavigation} profileReloadToken={companyProfileReload}
        templateSeed={documentTemplateSeed}
        onApproved={saved => {
          quoteReadVersion.current++; setQuote(saved); setEditingApproved(false);
          setHistoricalQuote(null); void load();
        }}/>
      {canWrite && <button type="button" className="button button-white"
        disabled={pending || !quote.source_request?.lines.length}
        onClick={useQuoteAsTemplate}>Создать КП по этому шаблону</button>}
      <QuoteSharePanel organizationId={organization.id} quote={quote} csrf={csrf} canWrite={canWrite}/>
      {canWrite && approvedReadOnly && <button type="button" className="button button-white"
        disabled={pending} onClick={() => {
          setEditingApproved(true);
          setNotice('Проверьте каждую строку и заново выберите товары перед сохранением новой версии.');
        }}>Изменить состав</button>}
      {quote.ready_for_draft ? <a className="button button-white"
        href={sessionApi.quotePdfPath(organization.id, quote.quote_id, quote.revision)}>
        <FileDown size={17} aria-hidden="true"/> {quote.approved ? 'Скачать КП PDF' : 'PDF черновика'}
      </a> : <p>PDF недоступен, пока строки КП требуют проверки
        или сумма превышает бюджет.</p>}
      {quote.revision > 1 && <div className="tenant-revision-history">
        <h3>Предыдущие версии КП</h3>
        <p>Исторические цены и товары доступны только для просмотра.
          При открытии версии редактор текущего КП не изменяется.</p>
        <label>Номер предыдущей версии
          <input type="number" min={1} max={quote.revision - 1}
            value={historyRevision}
            onChange={event => {
              historyReadVersion.current++;
              setHistoryLoading(false);
              setHistoricalQuote(null);
              setHistoryRevision(Number(event.target.value));
            }}/>
        </label>
        <button type="button" className="button button-white"
          disabled={historyLoading || !Number.isSafeInteger(historyRevision)
            || historyRevision < 1 || historyRevision >= quote.revision}
          onClick={() => void openHistoricalRevision()}>
          {historyLoading ? 'Загрузка версии…' : 'Открыть предыдущую версию'}
        </button>
      </div>}
      <button type="button" className="text-button" disabled={pending}
        onClick={startNewQuote}>
        Создать новое КП
      </button>
    </section>}

    {quote && historicalQuote && historicalQuote.quote_id === quote.quote_id
      && historicalQuote.revision < quote.revision &&
      <section className="access-catalog-state" aria-label="Предыдущая версия КП">
        <h2>Версия {historicalQuote.revision} — только просмотр</h2>
        <p>Версия прайса: {historicalQuote.catalog_version_id}</p>
        <p>Сумма КП: <strong>{money(historicalQuote.budget_exceeded
          ? historicalQuote.estimated_total_rub : historicalQuote.total_rub)}</strong></p>
        {historicalQuote.instructions && <p>
          Сохранённые условия: наценка {historicalQuote.instructions.rules.markup_percent}%,
          {' '}бюджет {money(historicalQuote.instructions.rules.budget_max_rub)}.
          {historicalQuote.instructions.rules.required_brand
            ? ' Только бренд: ' + historicalQuote.instructions.rules.required_brand + '.' : ''}
          {historicalQuote.instructions.rules.excluded_brands?.length
            ? ' Исключены: ' + historicalQuote.instructions.rules.excluded_brands.join(', ') + '.' : ''}
        </p>}
        {historicalQuote.base_total_rub !== null
          && historicalQuote.base_total_rub !== undefined &&
          <p>Сумма по прайсу: {money(historicalQuote.base_total_rub)}</p>}
        {historicalQuote.budget_exceeded && <p role="status">
          Предыдущая версия превышает ограничение бюджета.
        </p>}
        <ul className="tenant-list">
          {historicalQuote.lines.map(line => <li key={line.line_number}>
            <strong>{line.description}</strong> · артикул: {line.sku ?? 'не выбран'} ·
            {line.product_brand && <> Бренд: {line.product_brand} · </>}
            {line.supplier_id && <> Поставщик: {line.supplier_id} ·
              версия прайса: {line.source_catalog_version_id} ·
              SHA-256: {line.source_sha256} · </>}
            {' '}{line.sale_quantity ?? '—'} {line.sale_unit ?? ''} ×
            {' '}{line.unit_price_rub ?? '—'} ₽ =
            {' '}{line.line_total_rub ?? 'Нет цены'} ₽
            <RequirementMatrix checks={line.requirements}/>
          </li>)}
        </ul>
        {historicalQuote.ready_for_draft
          ? <a className="button button-white"
              href={sessionApi.quotePdfPath(organization.id,
                historicalQuote.quote_id, historicalQuote.revision)}>
              <FileDown size={17} aria-hidden="true"/>
              PDF версии {historicalQuote.revision}
            </a>
          : <p>Для этой версии PDF недоступен: строки требуют проверки.</p>}
      </section>}

    {compactNavigation && <details id="workspace-company" className="workspace-disclosure">
      <summary>Компания и оформление</summary>
      <div className="workspace-disclosure-body"><CompanyProfileSettings
        key={organization.id} organizationId={organization.id} organizationName={organization.name}
        csrf={csrf} canManage={isOwner} onSaved={() => setCompanyProfileReload(value => value + 1)}/></div>
    </details>}
    <details id="workspace-team" className="workspace-disclosure" open={!compactNavigation}>
      <summary><Users size={18} aria-hidden="true"/><span>Команда и доступ</span></summary>
      <div className="workspace-disclosure-body">
    <section className="access-catalog-state" aria-labelledby="invitations-title">
      <h2 id="invitations-title"><Users size={20} aria-hidden="true"/> Приглашения</h2>
      {invitations.length ? <ul className="tenant-list">
        {invitations.map(inv => <li key={inv.invitation_id}>
          <span>{inv.organization_name} — {memberRoleLabel(inv.proposed_role)}</span>
          <button className="button button-white" type="button"
            disabled={!csrf || pending}
            onClick={() => accept(inv.invitation_id)}>Принять</button>
        </li>)}
      </ul> : <p>Нет ожидающих приглашений.</p>}
    </section>

    {isOwner && <section className="access-catalog-state" aria-labelledby="team-title">
      <h2 id="team-title">Команда организации</h2>
      <form className="access-form" onSubmit={invite}>
        <label>Электронная почта сотрудника
          <input type="email" value={inviteEmail} required
            onChange={e => setInviteEmail(e.target.value)} placeholder="colleague@company.ru"/>
        </label>
        <label className="tenant-checkbox">
          <input type="checkbox" checked={inviteNew}
            onChange={e => setInviteNew(e.target.checked)}/>
          Новый сотрудник без аккаунта — отправить письмо
        </label>
        <label>Роль при приглашении
          <select value={inviteRole}
            onChange={e => setInviteRole(e.target.value as 'viewer' | 'editor')}>
            <option value="viewer">Только просмотр</option>
            <option value="editor">Менеджер — создание и редактирование КП</option>
          </select>
        </label>
        <button className="button button-primary" disabled={!csrf || pending} type="submit">
          <Plus size={17} aria-hidden="true"/> Пригласить
        </button>
      </form>
      <ul className="tenant-list">
        {members.map(member => <li key={member.user_id}>
          <span><strong>{member.email}</strong> — {memberRoleLabel(member.role)} ({member.status === 'active' ? 'доступ открыт' : 'доступ отозван'})</span>
          {member.status === 'active' && member.role !== 'owner' && <>
            <button type="button" className="button button-white"
              disabled={!csrf || pending}
              onClick={() => change(member, member.role === 'viewer' ? 'editor' : 'viewer')}>
              {member.role === 'viewer' ? 'Разрешить редактирование' : 'Оставить только просмотр'}
            </button>
            <button type="button" className="text-button danger"
              disabled={!csrf || pending} onClick={() => revoke(member)}>
              Отозвать доступ
            </button>
          </>}
        </li>)}
      </ul>
    </section>}

      </div>
    </details>
    {canWrite && <details id="workspace-sources" className="workspace-disclosure" open={!compactNavigation}>
      <summary><span>Прайсы и проверка данных</span></summary>
      <p>Подготовьте прайсы поставщиков, проверьте цены и характеристики, сравните сохранённые версии.</p>
      <div className="workspace-disclosure-body">
    {canWrite && <SupplierPrecisionReview
      key={'precision-' + organization.id} organizationId={organization.id} csrf={csrf}
      onCanonicalReady={onCanonicalReady}/>} 
    {canWrite && csrf && <SupplierCatalogChanges
      key={'catalog-diff-' + organization.id}
      organizationId={organization.id} csrf={csrf}
      />}
    {canWrite && <TenantCandidateReview
      organizationId={organization.id} csrf={csrf}
      canAdjudicate={organization.role === 'owner'}/>}

      {catalog && !catalogBlocked && !pending && editorRows.every(row => !row.llm)
        && validEditorRows && (!rulesText.trim()
          || (confirmedRules !== null && confirmedRules.raw_text === rulesText)) &&
        <SupplierQuoteComparison
          key={JSON.stringify([organization.id, catalog.version_id, csrf,
            editorRows.map(row => toLine(row.input)), confirmedRules])}
          organizationId={organization.id}
          activeVersionId={catalog.version_id}
          csrf={csrf!}
          requestLines={editorRows.map(row => toLine(row.input))}
          instructions={confirmedRules ?? undefined}
          onSaved={saved => {
            quoteReadVersion.current++;
            historyReadVersion.current++;
            setHistoricalQuote(null);
            setQuote(saved);
            setManagerAudit(null);
            setReviewAudit(null);
            setNotice('Сохранено КП с подтверждённым выбором нескольких поставщиков.');
            void load();
          }}
        />}
      {editorRows.some(row => row.llm) && <p>Подбор по нескольким поставщикам пока
        доступен для позиций, введённых вручную. Для распознанных ИИ
        строк нужно отдельно сохранить результат проверки менеджером.</p>}
      </div>
    </details>}
  </div>;
}
