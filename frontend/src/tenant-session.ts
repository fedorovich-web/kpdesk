/** Cookie-based, same-origin HTTPS tenant session API. No localStorage tokens. */
import type { ConfirmedInstructions, InstructionProposal } from './domain';
export class SessionApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number) {
    super(message);
    this.name = 'SessionApiError';
  }
}

export type TrainingFeedbackContext = {
  quote_id: string; source_revision: number; source_snapshot_sha256: string; catalog_version_id: string;
  lines: Array<{ line_number: number; description: string; selected_sku: string | null;
    no_match_allowed: boolean; candidates: Array<{ sku: string; name: string; requirements?: RequirementCheck[] }> }>;
};
export type TrainingFeedbackDecision = {
  line_number: number; relevant_sku: string | null; rejected_skus: string[]; no_match: boolean;
};
export type TrainingFeedbackBundle = {
  format: 'kpdesk-manager-feedback-v1'; training_data_kind: 'manager_reviewed';
  organization_id: string; quote_id: string; revision: number; snapshot_sha256: string;
  catalog_version_id: string; training_consent: boolean; catalog: unknown[]; events: unknown[];
};
export type TrainingFeedbackSummary = {
  feedback_id: string; quote_id: string; revision: number; reviewer_id: string;
  created_at: string; withdrawn_at: string | null; event_count: number; curation_status: 'pending_review';
};
export type TrainingFeedbackHistory = { items: TrainingFeedbackSummary[]; next_cursor: string | null };

export type CatalogPreflight = {
  source_sha256: string; publishable: boolean; rows_count: number;
  total_errors: number;
  errors: Array<{ code: string; message: string; row: number | null; field: string | null }>;
};

export type SupplierMappingProfile = {
  found: boolean;
  source_sha256: string;
  profile_sha256: string | null;
  fingerprint: string | null;
  approved_by: string | null;
  price_basis: string | null;
  columns: Record<string, number> | null;
  header_row: number | null;
  sheet: string | null;
  reviewed: boolean;
  canonical_importable: false;
  model_training_authorized: false;
};
export type SupplierPriceBasis =
  'standard' | 'retail' | 'wholesale' | 'rrp' | 'vat_included';

export type SupplierCanonicalDecision = {
  line: number; source_row_sha256: string; confirmed: true;
} & ({ action: 'exclude'; reason: string } | {
  action: 'include'; category: string;
  sale_unit: 'штука' | 'пачка' | 'коробка';
  pieces_per_sale_unit: number;
  attributes: Record<string, string | number | string[]>;
  brand: string | null; paper_format: string | null; paper_gsm: number | null;
  pen_ink_color: string | null; pen_ink_type: string | null;
  price_unit_review_evidence: string | null;
  precision_review: {
    price_rub: string; rounding_mode: 'half_up' | 'half_even' | 'down' | 'up';
    explanation: string;
  } | null;
});

export type SupplierCanonicalReview = {
  source_sha256: string; mapping: SupplierPrecisionMapping;
  formula_policy: 'reject' | 'quarantine_rows'; price_basis: SupplierPriceBasis;
  explicitly_confirmed_price_basis: true; decisions: SupplierCanonicalDecision[];
};

export type SupplierMappingHistoryItem = {
  profile_sha256: string;
  fingerprint: string;
  source_format: string;
  columns: Record<string, number | string>;
  header_row: number | null;
  sheet: string | null;
  delimiter: string | null;
  encoding: string | null;
  items_key: string | null;
  confirmed_currency: 'RUB' | null;
  price_basis: SupplierPriceBasis;
  approved_by: string;
  approved_source_sha256: string;
  review_evidence: string;
  reviewed_at: string;
};
export type SupplierMappingHistory = {
  items: SupplierMappingHistoryItem[];
  next_offset: number | null;
  canonical_importable: false;
  model_training_authorized: false;
};

export type SupplierSourceInspection = {
  source_sha256: string; source_format: string;
  row_count: number;
  available_sheets: string[];
  selected_sheet: string | null;
  encoding: string | null; delimiter: string | null;
  items_key: string | null; header_row: number | null;
  observed_columns: string[];
  sample_rows: string[][];
  proposed_fields: Record<string, string | number>;
  options: Record<string, Array<string | number>>;
  stage_mapping: (SupplierPrecisionMapping & { source_sha256?: string | null }) | null;
  issues: string[];
  requires_confirmation: true;
  canonical_importable: false;
  model_training_authorized: false;
};

export type SupplierPrecisionMapping = {
  name: number; price: number; header_row: number;
  source_sha256?: string | null;
  sku?: number | null; currency?: number | null;
  category?: number | null; sale_unit?: number | null;
  pieces_per_sale_unit?: number | null;
  confirmed_currency?: 'RUB' | null;
  sheet?: string | null; delimiter?: ';' | ',' | '\t';
  encoding?: 'utf-8' | 'utf-8-sig' | 'cp1251';
};
export type SupplierPrecisionCase = {
  line: number; source_row_sha256: string; supplier_sku: string | null;
  name: string; source_price_raw: string; source_sale_unit: string | null;
  source_pack_size: number | null; pack_size_review_reason: string | null;
  currency_confirmed_rub: boolean;
  formula_provenance_unverified: boolean; issues: string[];
};
export type SupplierPrecisionPreview = {
  source_sha256: string; source_filename: string;
  total_rows: number; precision_cases: number;
  offset: number; next_offset: number | null;
  rows: SupplierPrecisionCase[];
  published: false; model_training_authorized: false;
};
export type SupplierStageRow = {
  line: number; source_row_sha256: string;
  supplier_sku: string | null; name: string;
  price_rub: string | null; source_high_precision_price?: string | null;
  source_category: string | null;
  source_sale_unit: string | null; source_pack_size: number | null;
  issues: string[]; pack_size_review_reason: string | null;
};
export type SupplierStagePreview = {
  source_sha256: string; total_rows: number; priced_rub_rows: number;
  unresolved_rows: number; pack_size_issue_counts: Record<string, number>;
  offset: number; next_offset: number | null; sampled_rows: SupplierStageRow[];
  requires_confirmation: true; canonical_importable: false;
  model_training_authorized: false;
};
export type CandidateReviewTask = {
  request_id: string; task_sha256: string; request_text: string;
  partition: 'train' | 'validation'; source_sha256: string; source_batch_id: string;
  candidate_skus: string[];
  candidates: Array<{
    sku: string; name: string;
    sale_unit: 'штука' | 'пачка' | 'коробка'; pieces_per_sale_unit: number;
  }>;
};
export type CandidateInspectResponse = {
  archive_sha256: string; queue_sha256: string; total_requests: number;
  offset: number; next_offset: number | null; tasks: CandidateReviewTask[];
  rights_verified: false; privacy_review_complete: false;
  independent_gold: false; training_approved: false;
};
export type CandidateReviewReceipt = {
  recorded: boolean; queue_sha256: string; task_sha256: string;
  request_id: string; reviewer_user_id: string; rights_verified: false;
  independently_verified: false; privacy_review_complete: false;
  independent_gold: false; training_approved: false;
  benchmark_approved: false;
};
export type CandidateHistory = {
  rows: Array<{
    request_id: string; task_sha256: string; reviewer_user_id: string;
    candidate_skus: string[]; accepted_skus: string[]; rejected_skus: string[];
    unresolved_skus: string[]; no_candidate_match: boolean;
    rationale: string; recorded_at: string;
  }>;
  next_offset: number | null; independent_gold: false; training_approved: false;
};
export type CandidateConsensus = {
  queue_sha256: string; source_bound_review_count: number;
  agreed: Array<{
    request_id: string; task_sha256: string;
    reviewer_count: number; reviewer_user_ids: string[];
    accepted_skus: string[]; rejected_skus: string[];
    no_candidate_match_among_proposals: boolean;
  }>;
  conflicts: Array<{
    request_id: string; task_sha256: string;
    reviewer_count: number; reviewer_user_ids: string[];
  }>;
  incomplete: Array<{
    request_id: string; task_sha256: string;
    reviewer_count: number; reviewer_user_ids: string[];
  }>;
  rights_verified: false; privacy_review_complete: false;
  independent_gold: false; training_approved: false; benchmark_approved: false;
};
export type SourceRestrictionFinding = 'requires_evidence' | 'restricted' | 'rejected';
export type SourceRestrictionHistory = {
  rows: Array<{
    source_sha256: string; review_kind: 'rights' | 'privacy';
    finding: SourceRestrictionFinding; evidence_sha256: string | null;
    rationale: string; reviewer_user_id: string; recorded_at: string;
  }>;
  next_offset: number | null; rights_verified: false;
  privacy_review_complete: false; training_approved: false;
};
export type CandidateAdjudicationFinding =
  'resolve_proposals' | 'needs_evidence' | 'reject_case';
export type CandidateAdjudicationHistory = {
  rows: Array<{
    task_sha256: string; source_sha256: string; request_id: string;
    candidate_skus: string[]; finding: CandidateAdjudicationFinding;
    accepted_skus: string[]; rejected_skus: string[];
    no_candidate_match: boolean; rationale: string;
    adjudicator_user_id: string; original_reviewer_count: number;
    recorded_at: string;
  }>;
  next_offset: number | null; independent_gold: false; training_approved: false;
};
export type CandidateJudgment = 'accepted' | 'rejected' | 'unresolved';
export type SupplierUnitFinding =
  'confirm_price_unit' | 'needs_supplier_confirmation' | 'reject_source_unit';
export type SupplierUnitDecision = {
  source_row_sha256: string; line: number; finding: SupplierUnitFinding;
  approved_sale_unit: 'штука' | 'пачка' | 'коробка' | null;
  approved_pack_size: number | null; rationale: string;
};
export type SupplierUnitReviewRecord = {
  source_line: number; source_row_sha256: string;
  source_sale_unit: string | null; source_pack_size: number | null;
  finding: SupplierUnitFinding; approved_sale_unit: string | null;
  approved_pack_size: number | null; rationale: string;
  reviewer_user_id: string; reviewed_at: string;
};
export type SupplierUnitHistory = {
  records: SupplierUnitReviewRecord[]; next_offset: number | null;
  published: false; model_training_authorized: false;
};
export type SupplierPrecisionFinding =
  'approve_rounding' | 'needs_supplier_confirmation' | 'reject_source_price';
export type SupplierRoundingMode = 'half_up' | 'half_even' | 'down' | 'up';
export type SupplierPrecisionDecision = {
  line: number; source_row_sha256: string;
  finding: SupplierPrecisionFinding; rounding_mode: SupplierRoundingMode | null;
  rounded_price_rub: string | null; rationale: string;
};
export type SupplierPrecisionAudit = {
  source_line: number; source_row_sha256: string; source_price_raw: string;
  finding: SupplierPrecisionFinding; rounding_mode: SupplierRoundingMode | null;
  rounded_price_rub: string | null; rationale: string;
  reviewer_user_id: string; reviewed_at: string;
};
export type SupplierPrecisionHistory = {
  records: SupplierPrecisionAudit[];
  published: false; current_price_verified: false;
};

export type TenantOrganization = { id: string; name: string; role: 'owner' | 'editor' | 'viewer' };
export type ActiveTenantCatalog = { version_id: string; source_sha256: string; rows_count: number };
export type CatalogVersionHistory = { items: ActiveTenantCatalog[]; next_offset: number | null };
export type SupplierCatalogChange = {
  sku: string;
  kind: 'added' | 'removed' | 'price_changed' | 'product_changed'
    | 'product_and_price_changed';
  changed_fields: string[];
  before_price_rub: string | null;
  after_price_rub: string | null;
  comparable_price_delta_rub: string | null;
};
export type SupplierCatalogDiff = {
  supplier_id: string;
  before_catalog_version_id: string; after_catalog_version_id: string;
  before_source_sha256: string; after_source_sha256: string;
  counts: Record<SupplierCatalogChange['kind'], number>;
  total_changes: number; offset: number; next_offset: number | null;
  requires_manager_review: true;
  changes: SupplierCatalogChange[];
};

export type HistoricalSupplierSourceRef = {
  supplier_id: string; profile_sha256: string; catalog_version_id: string;
};
export type HistoricalSupplierSource = HistoricalSupplierSourceRef & {
  profile_fingerprint: string; source_sha256: string; rows_count: number;
  evidence: 'original';
};
export type HistoricalSupplierSourceListing = {
  supplier_id: string; options: HistoricalSupplierSource[];
  truncated: boolean; audit_only: true;
};

export type SupplierSourceRef = {
  supplier_id: string; profile_fingerprint: string; catalog_version_id: string;
};
export type DiscoveredSupplierSource = SupplierSourceRef & {
  source_sha256: string; rows_count: number;
  evidence: 'original' | 'reviewed_canonical';
};
export type SupplierSourceDiscovery = {
  suppliers: Array<{
    supplier_id: string; options: DiscoveredSupplierSource[];
    truncated: boolean; canonical_discovery_available: boolean;
  }>;
};
export type SupplierSelection = {
  line_number: number; supplier_id: string; catalog_version_id: string; sku: string;
};
export type SupplierQuotePreview = {
  requires_manager_confirmation: true; catalog_source_count: number;
  lines: Array<{
    line_number: number; description: string;
    status: 'select_supplier' | 'no_verified_offer' | 'request_needs_review';
    offers: Array<{
      supplier_id: string; catalog_version_id: string; source_sha256: string;
      sku: string; product_name: string; product_brand: string | null;
      sale_quantity: number; sale_unit: string; unit_price_rub: string;
      base_total_rub: string; selling_total_rub: string;
    }>;
  }>;
};

export type TenantInvitation = {
  invitation_id: string;
  organization_id: string;
  organization_name: string;
  proposed_role: 'viewer' | 'editor';
  expires_at: string;
};
export type TenantMember = {
  user_id: string; email: string; role: TenantOrganization['role'];
  status: 'active' | 'revoked';
};
export type QuoteLineInput = {
  source_quote: string; description: string;
  category: string | null; quantity: number | null;
  unit: string | null; brand: string | null;
  paper_format: string | null; paper_gsm: number | null;
  pen_ink_color: string | null; pen_ink_type: string | null;
  attributes: Record<string, string | number | string[] | null>;
};
export type TenantExtractionPreview = {
  extraction_id: string;
  extracted_request: { lines: QuoteLineInput[]; instruction_quotes?: string[] };
  requires_human_review: true;
  source: 'openrouter' | 'groq';
  approved: false;
  cache_hit?: boolean;
  instruction_proposal?: InstructionProposal | null;
  analysis?: RequestAnalysis;
};

export type RequirementCheck = {
  field: string; label: string; requested: string | null; actual: string | null;
  status: 'provided' | 'matched' | 'mismatch' | 'missing_request' | 'unknown_product' | 'needs_review';
  note?: string | null;
};
export type RequestAnalysis = {
  ready_for_matching: boolean; questions_text: string; scope_notice: string;
  lines: Array<{ line_number: number; source_quote: string; description: string;
    missing_fields: string[]; questions: string[] }>;
};

export type TenantCategorySuggestion = {
  original_category: string | null;
  suggested_category: string | null;
  best_category: string;
  confidence: number;
  margin: number;
  abstained: boolean;
  requires_explicit_acceptance: true;
};
export type TenantMatchCandidate = {
  sku: string; name: string; price_rub: string;
  sale_unit: 'штука' | 'пачка' | 'коробка';
  pieces_per_sale_unit: number; score: number;
  status: 'compatible' | 'needs_review'; reason_codes: string[];
  requirements?: RequirementCheck[];
};
export type TenantMatchResponse = {
  catalog_version_id: string; mode: 'ml' | 'rapidfuzz' | 'tfidf';
  requires_manager_confirmation: true; calibrated_no_match: false;
  candidates: TenantMatchCandidate[];
};
export type TenantBatchMatchResponse = {
  catalog_version_id: string; mode: TenantMatchResponse['mode'];
  lines: Array<TenantMatchResponse & { line_number: number }>;
};
export type ManagerDecisionHistory = {
  quote_id: string;
  revision: number;
  items: Array<{
    line_number: number;
    sku: string;
    catalog_version_id: string;
    approved_by_user_id: string;
    recorded_at: string;
  }>;
};
export type ManagerSkuSelection = {
  line_number: number; sku: string;
  approved_sale_quantity: number | null;
};

export type CompositionVariant = {
  kind: 'relevance' | 'lower_cost';
  lines: Array<{ line_number: number; sku: string; name: string;
    sale_quantity: number; sale_unit: string; line_total_rub: string }>;
  unresolved_line_numbers: number[];
  total_rub: string | null;
  budget_exceeded: boolean;
  selectable: boolean;
};
export type TenantVariantsResponse = TenantBatchMatchResponse & {
  candidate_limit_per_line: number;
  variants: CompositionVariant[];
};

export type ReviewEvidence = {
  extraction_id: string;
  source_text: string;
  original_extracted_request: { lines: QuoteLineInput[] };
  reviewed_lines: boolean[];
};
export type QuoteReviewAudit = {
  reviewer_user_id: string; reviewed_at: string; extraction_id: string;
  source_sha256: string; preview_sha256: string;
  reviewed_request_sha256: string; reviewed_line_hashes: string[];
};

export type TenantQuote = {
  quote_id: string; revision: number; catalog_version_id: string;
  ready_for_draft: boolean; total_rub: string | null;
  lines: Array<{
    line_number: number; description: string; status: string;
    sku: string | null; product_name: string | null;
    product_brand?: string | null;
    supplier_id?: string | null;
    source_catalog_version_id?: string | null;
    source_sha256?: string | null;
    requested_quantity: number | null; requested_unit: string | null;
    sale_quantity: number | null; sale_unit: string | null;
    unit_price_rub: string | null; line_total_rub: string | null;
    reason_codes: string[];
    requirements?: RequirementCheck[];
  }>;
  snapshot_sha256: string;
  approved: boolean;
  source_request?: { lines: QuoteLineInput[] } | null;
  instructions?: ConfirmedInstructions | null;
  base_total_rub?: string | null;
  estimated_total_rub?: string | null;
  budget_exceeded?: boolean;
  document?: CommercialDocument | null;
  approval?: { user_id: string; approved_at: string; source_revision: number; source_snapshot_sha256: string } | null;
};
export type ProposalNarrative = {
  headline: string; opening: string; solution: string; closing: string;
};
export type ProposalContext = {
  issuer_name: string; recipient_name: string; manager_brief: string; commercial_terms: string;
};
export type CompanyProfileData = {
  schema_version: 1; display_name: string; about: string; advantages: string;
  contacts: string; default_terms: string; generation_style: string;
  issuer_details?: PartyDetails | null;
  logo?: CompanyLogo | null;
};
export type CompanyLogo = { mime_type: 'image/png'; width: number; height: number; sha256: string; data_base64: string };
export type CompanyProfileSnapshot = { revision: number; sha256: string; data: CompanyProfileData };
export type CommercialDocument = ProposalContext & {
  issued_on: string; narrative: ProposalNarrative; company_profile?: CompanyProfileSnapshot | null;
  alternatives?: DocumentAlternative[];
  appearance?: DocumentAppearance | null;
  recipient_details?: PartyDetails | null;
};
export type DocumentAppearance = {
  template: 'presentation' | 'compact'; accent_color: string; show_company_section: boolean;
  layout_version?: 1 | 2;
  default_logo?: 'kpdesk_v1' | null;
};
export type ProposalTemplateSeed = { appearance: DocumentAppearance | null; commercial_terms: string };
export type PartyDetails = {
  full_name: string; short_name: string; inn: string; kpp: string | null;
  ogrn: string | null; address: string | null; status: string | null;
};
export type PartyLookupResponse = {
  party: PartyDetails | null; source: 'dadata'; fetched_at: string; cached: boolean;
};
export type AlternativeSelection = { kind: 'relevance' | 'lower_cost'; selections: ManagerSkuSelection[] };
export type DocumentAlternative = {
  selection: AlternativeSelection; catalog_version_id: string;
  lines: TenantQuote['lines']; total_rub: string;
};
export type DocumentAlternativePreview = {
  quote_id: string; source_revision: number; source_snapshot_sha256: string;
  candidate_limit_per_line: number; alternatives: DocumentAlternative[];
};
export type PresentationPreview = {
  quote_id: string; source_revision: number; source_snapshot_sha256: string;
  narrative: ProposalNarrative; model: string; requires_human_review: boolean;
  company_profile?: CompanyProfileSnapshot | null;
  alternatives?: DocumentAlternative[];
};
export type QuoteListItem = {
  quote_id: string; revision: number; total_rub: string | null;
  ready_for_draft: boolean;
  approved?: boolean;
};
export type QuoteListPage = { items: QuoteListItem[]; next_cursor?: string | null };

async function call<T>(
  path: string, options: RequestInit = {}, timeoutMs = 35_000,
  binary = false,
): Promise<T> {
  if (!path.startsWith('/api/session/') || path.includes('..') || path.includes('://')) {
    throw new Error('Некорректный адрес серверной сессии.');
  }
  if (typeof window !== 'undefined' && window.location.protocol !== 'https:') {
    throw new SessionApiError(
      'https_required', 'Для входа в KPDesk нужно защищённое соединение HTTPS.', 0,
    );
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...options,
      credentials: 'include',
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(options.headers ?? {}),
      },
    });
    const payload: unknown = response.ok && binary ? await response.blob()
      : response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const error = payload as { code?: unknown; message?: unknown } | null;
      throw new SessionApiError(
        typeof error?.code === 'string' ? error.code : 'http_error',
        typeof error?.message === 'string'
          ? error.message : 'Запрос завершился ошибкой (HTTP ' + response.status + '). Проверьте результат операции перед повторной попыткой.',
        response.status,
      );
    }
    if (response.status !== 204 && (!payload || typeof payload !== 'object')) {
      throw new SessionApiError('invalid_response', 'Не удалось прочитать ответ KPDesk. Проверьте результат операции перед повторной попыткой.', response.status);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof SessionApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') {
      throw new SessionApiError('timeout', 'KPDesk не ответил вовремя. Проверьте результат операции перед повторной попыткой.', 0);
    }
    throw new SessionApiError('network', 'Не удалось получить ответ от KPDesk. Проверьте подключение и результат операции перед повторной попыткой.', 0);
  } finally {
    clearTimeout(timeout);
  }
}

const precisionPath = (organizationId: string, supplierId: string) =>
  '/api/session/organizations/' + encodeURIComponent(organizationId) +
  '/suppliers/' + encodeURIComponent(supplierId) + '/precision-reviews';

const json = (payload: object): RequestInit => ({ method: 'POST', body: JSON.stringify(payload) });

// Preserve the operation identity after uncertain network/timeout outcomes.
// Bound memory use; no request content or key is persisted in browser storage.
const pendingLlmOperations = new Map<string, string>();
async function callLlm<T>(path: string, payload: object, csrf: string): Promise<T> {
  const fingerprint = path + '\n' + JSON.stringify(payload);
  let key = pendingLlmOperations.get(fingerprint);
  if (!key) {
    key = crypto.randomUUID();
    if (pendingLlmOperations.size >= 32) {
      throw new SessionApiError('llm_operations_pending', 'Есть незавершённые запросы к ИИ. Проверьте их результат перед новой генерацией.', 0);
    }
    pendingLlmOperations.set(fingerprint, key);
  }
  try {
    const result = await call<T>(path, {
      ...json(payload), headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
    }, 135_000);
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      throw new SessionApiError('invalid_response', 'Не удалось прочитать ответ ИИ. Проверьте результат запроса перед повторной генерацией.', 200);
    }
    pendingLlmOperations.delete(fingerprint);
    return result;
  } catch (error) {
    if (error instanceof SessionApiError && error.status > 0 &&
      !['provider_timeout', 'provider_unreachable', 'duplicate_inference_in_progress',
        'quota_ledger_unavailable', 'invalid_response', 'http_error'].includes(error.code)) {
      pendingLlmOperations.delete(fingerprint);
    }
    throw error;
  }
}

export type QuoteShare = {
  id: string; revision: number; created_at: string; expires_at: string; revoked_at: string | null;
};
export const sessionApi = {
  quoteShares: (org: string, quoteId: string) => call<{ available: boolean; shares: QuoteShare[] }>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quoteId) + '/shares'),
  createQuoteShare: (org: string, quote: TenantQuote, days: number, csrf: string) =>
    call<{ share: QuoteShare; token: string }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id) + '/shares',
      { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256, days }),
        headers: { 'X-CSRF-Token': csrf } }),
  revokeQuoteShare: (org: string, quoteId: string, shareId: string, csrf: string) => call<null>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quoteId)
      + '/shares/' + encodeURIComponent(shareId) + '/revoke',
    { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  login: (email: string, password: string) =>
    call<{ csrf_token: string; expires_at: string }>('/api/session/login', json({ email, password })),
  register: (email: string, password: string, organization_name: string) =>
    call<{ status: 'accepted' }>('/api/session/register', json({ email, password, organization_name })),
  resend: (email: string, password: string) =>
    call<{ status: 'accepted' }>('/api/session/resend-confirmation', json({ email, password })),
  verify: (token: string) =>
    call<{ user_id: string; organization_id: string }>('/api/session/verify-email', json({ token })),
  forgot: (email: string) =>
    call<{ status: 'accepted' }>('/api/session/password/forgot', json({ email })),
  reset: (token: string, password: string) =>
    call<null>('/api/session/password/reset', json({ token, password })),
  me: () => call<{ user_id: string }>('/api/session/me'),
  resume: () => call<{ user_id: string; csrf_token: string }>('/api/session/resume'),
  organizations: () => call<{ organizations: TenantOrganization[] }>('/api/session/organizations'),
  currentCatalog: (organizationId: string) =>
    call<ActiveTenantCatalog>(
      '/api/session/organizations/' + encodeURIComponent(organizationId) + '/catalog/current',
    ),
  catalogVersions: (organizationId: string, offset = 0) =>
    call<CatalogVersionHistory>(
      '/api/session/organizations/' + encodeURIComponent(organizationId)
      + '/catalog/versions?offset=' + encodeURIComponent(String(offset)),
    ),
  supplierHistoricalSources: (organizationId: string, supplierId: string, csrf: string) =>
    call<HistoricalSupplierSourceListing>(
      '/api/session/organizations/' + encodeURIComponent(organizationId)
      + '/supplier-price-history/sources', {
        ...json({ supplier_id: supplierId }),
        headers: { 'X-CSRF-Token': csrf },
      }, 60_000,
    ),
  compareHistoricalSupplierVersions: (
    organizationId: string, before: HistoricalSupplierSourceRef,
    after: HistoricalSupplierSourceRef, csrf: string, offset = 0, limit = 50,
  ) => call<SupplierCatalogDiff>(
    '/api/session/organizations/' + encodeURIComponent(organizationId)
    + '/supplier-price-history/diff', {
      ...json({ before, after, offset, limit }),
      headers: { 'X-CSRF-Token': csrf },
    }, 60_000,
  ),
  supplierVersions: (organizationId: string, supplierId: string, csrf: string) =>
    call<SupplierSourceDiscovery['suppliers'][number]>(
      '/api/session/organizations/' + encodeURIComponent(organizationId)
      + '/quotes/supplier-comparison/supplier-sources', {
        ...json({ supplier_id: supplierId }),
        headers: { 'X-CSRF-Token': csrf },
      }, 60_000,
    ),
  compareSupplierVersions: (
    organizationId: string, before: SupplierSourceRef, after: SupplierSourceRef,
    csrf: string, offset = 0, limit = 50,
  ) => call<SupplierCatalogDiff>(
    '/api/session/organizations/' + encodeURIComponent(organizationId)
    + '/quotes/supplier-comparison/source-diff', {
      ...json({ before, after, offset, limit }),
      headers: { 'X-CSRF-Token': csrf },
    }, 60_000,
  ),
  discoverSupplierSources: (
    organizationId: string, supplierIds: string[], csrf: string,
  ) => call<SupplierSourceDiscovery>(
    '/api/session/organizations/' + encodeURIComponent(organizationId)
    + '/quotes/supplier-comparison/sources', {
      ...json({ supplier_ids: supplierIds }),
      headers: { 'X-CSRF-Token': csrf },
    }, 60_000,
  ),
  previewSupplierQuote: (
    organizationId: string, sources: SupplierSourceRef[],
    lines: QuoteLineInput[], csrf: string, instructions?: ConfirmedInstructions,
  ) => call<SupplierQuotePreview>(
    '/api/session/organizations/' + encodeURIComponent(organizationId)
    + '/quotes/supplier-comparison/preview', {
      ...json({ sources, extracted_request: { lines }, instructions }),
      headers: { 'X-CSRF-Token': csrf },
    }, 60_000,
  ),
  createSupplierQuote: (
    organizationId: string, activeVersion: string, sources: SupplierSourceRef[],
    lines: QuoteLineInput[], selections: SupplierSelection[], csrf: string,
    key: string, instructions?: ConfirmedInstructions,
  ) => call<TenantQuote>(
    '/api/session/organizations/' + encodeURIComponent(organizationId)
    + '/quotes/supplier-comparison', {
      ...json({
        sources, active_catalog_version_id: activeVersion,
        extracted_request: { lines }, instructions, selections,
      }), headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
    }, 60_000,
  ),
  appendReviewedSupplierCheckpoint: (
    org: string, supplier: string, file: File, csrf: string,
    review: SupplierCanonicalReview, idempotencyKey: string,
    expectedCheckpointId: string | null, replaceExisting = false,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      ...review, idempotency_key: idempotencyKey,
      expected_checkpoint_id: expectedCheckpointId,
      replace_existing: replaceExisting,
    }));
    return call<{
      checkpoint_id: string; source_sha256: string;
      mapping_sha256: string; taxonomy_version: string;
      recorded: true; published: false;
    }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/suppliers/'
      + encodeURIComponent(supplier) + '/canonical/checkpoints/append',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  restoreReviewedSupplierCheckpointPage: (
    org: string, supplier: string, file: File, csrf: string,
    review: Pick<SupplierCanonicalReview,
      'source_sha256' | 'mapping' | 'formula_policy' | 'price_basis'>,
    offset: number, expectedCheckpointId: string | null, limit = 50,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      source_sha256: review.source_sha256, mapping: review.mapping,
      formula_policy: review.formula_policy, price_basis: review.price_basis,
      offset, limit, expected_checkpoint_id: expectedCheckpointId,
    }));
    return call<{
      checkpoint_id: string; source_sha256: string; mapping_sha256: string;
      taxonomy_version: string; price_basis: SupplierPriceBasis;
      reviewer_user_id: string; total_count: number; offset: number;
      limit: number; next_offset: number | null;
      decisions: SupplierCanonicalDecision[]; published: false;
    } | null>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/suppliers/'
      + encodeURIComponent(supplier) + '/canonical/checkpoints/restore-page',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  saveReviewedSupplierCheckpoint: (
    org: string, supplier: string, file: File, csrf: string,
    review: SupplierCanonicalReview, idempotencyKey: string,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      ...review, idempotency_key: idempotencyKey, expected_checkpoint_id: null,
    }));
    return call<{
      checkpoint_id: string; source_sha256: string;
      mapping_sha256: string; taxonomy_version: string;
      recorded: true; published: false;
    }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/suppliers/'
      + encodeURIComponent(supplier) + '/canonical/checkpoints/save',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  publishReviewedSupplierCheckpoint: (
    org: string, supplier: string, checkpointId: string,
    file: File, csrf: string, review: SupplierCanonicalReview,
    canonicalSha256: string, idempotencyKey: string,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      source_sha256: review.source_sha256,
      mapping: review.mapping, formula_policy: review.formula_policy,
      price_basis: review.price_basis,
    }));
    return call<ActiveTenantCatalog>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/suppliers/'
      + encodeURIComponent(supplier) + '/canonical/checkpoints/'
      + encodeURIComponent(checkpointId) + '/publish', {
        method: 'POST', body: form, headers: {
          'X-CSRF-Token': csrf, 'X-Catalog-SHA256': canonicalSha256,
          'Idempotency-Key': idempotencyKey,
        },
      }, 60_000,
    );
  },
  exportReviewedSupplier: async (
    org: string, supplier: string, file: File, csrf: string,
    review: SupplierCanonicalReview,
  ): Promise<File> => {
    if (window.location.protocol !== 'https:') {
      throw new SessionApiError('https_required', 'Требуется защищённое соединение.', 0);
    }
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify(review));
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60_000);
    try {
      const response = await fetch(
        '/api/session/organizations/' + encodeURIComponent(org) + '/suppliers/'
        + encodeURIComponent(supplier) + '/canonical/export', {
          method: 'POST', body: form, cache: 'no-store', credentials: 'include',
          headers: { 'X-CSRF-Token': csrf }, signal: controller.signal,
        },
      );
      if (!response.ok) {
        const detail: unknown = await response.json().catch(() => null);
        const issue = detail as { message?: unknown; code?: unknown; row?: unknown } | null;
        const description = typeof issue?.message === 'string'
          ? issue.message : 'Проверенные строки отклонены сервером.';
        throw new SessionApiError(
          typeof issue?.code === 'string' ? issue.code : 'invalid_canonical_review',
          typeof issue?.row === 'number' ? description + ' Строка ' + issue.row : description,
          response.status,
        );
      }
      const expected = response.headers.get('X-Catalog-SHA256');
      const sourceSha = response.headers.get('X-Source-SHA256');
      if (!expected || !/^[a-f0-9]{64}$/.test(expected)
        || sourceSha !== review.source_sha256
        || !response.headers.get('content-type')?.includes('text/csv')) {
        throw new SessionApiError('invalid_catalog_export', 'Получен файл неподходящего формата. Повторите скачивание каталога.', 0);
      }
      const bytes = await response.arrayBuffer();
      if (!bytes.byteLength || bytes.byteLength > 10 * 1024 * 1024
        || !crypto.subtle) {
        throw new SessionApiError('invalid_catalog_export', 'Размер или формат файла не соответствует каталогу. Повторите скачивание.', 0);
      }
      const digest = await crypto.subtle.digest('SHA-256', bytes);
      const actual = Array.from(new Uint8Array(digest))
        .map(value => value.toString(16).padStart(2, '0')).join('');
      if (actual !== expected) {
        throw new SessionApiError('invalid_catalog_export', 'Проверка целостности файла не пройдена. Скачайте каталог заново.', 0);
      }
      return new File([bytes], 'reviewed-supplier.csv', { type: 'text/csv' });
    } catch (cause) {
      if (cause instanceof SessionApiError) throw cause;
      if (cause instanceof Error && cause.name === 'AbortError') {
        throw new SessionApiError('timeout', 'Время проверки прайса истекло.', 0);
      }
      throw new SessionApiError('network', 'Не удалось получить проверенный каталог.', 0);
    } finally {
      clearTimeout(timeout);
    }
  },
  preflightCatalog: (organizationId: string, file: File, csrf: string) => {
    const form = new FormData();
    form.append('file', file);
    return call<CatalogPreflight>(
      '/api/session/organizations/' + encodeURIComponent(organizationId) + '/catalog/preflight',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  importCatalog: (organizationId: string, file: File, csrf: string, key: string,
    expectedSha?: string) => {
    const form = new FormData();
    form.append('file', file);
    return call<ActiveTenantCatalog>(
      '/api/session/organizations/' + encodeURIComponent(organizationId) + '/catalog/import',
      {
        method: 'POST', body: form,
        headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key,
          ...(expectedSha ? { 'X-Source-SHA256': expectedSha } : {}) },
      },
      60_000,
    );
  },
  supplierMappingHistory: (org: string, supplier: string, offset = 0) =>
    call<SupplierMappingHistory>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/suppliers/' + encodeURIComponent(supplier) + '/mapping-profiles/history' +
      '?offset=' + encodeURIComponent(String(offset)),
    ),
  matchSupplierMapping: (
    org: string, supplier: string, file: File, csrf: string, sheet?: string,
  ) => {
    const form = new FormData();
    form.append('file', file);
    if (sheet) form.append('sheet', sheet);
    return call<SupplierMappingProfile>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/suppliers/' + encodeURIComponent(supplier) + '/mapping-profiles/match',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  saveSupplierMapping: (
    org: string, supplier: string, file: File, csrf: string,
    inspection: SupplierSourceInspection, mapping: SupplierPrecisionMapping,
    priceBasis: SupplierPriceBasis, evidence: string,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('approval_json', JSON.stringify({
      source_sha256: inspection.source_sha256,
      mapping, price_basis: priceBasis,
      confirmed_currency: mapping.confirmed_currency ?? null,
      evidence, explicitly_confirmed: true,
    }));
    return call<SupplierMappingProfile>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/suppliers/' + encodeURIComponent(supplier) + '/mapping-profiles',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  inspectSupplierStructure: (
    org: string, supplier: string, file: File, csrf: string,
    sheet?: string, quarantineFormulas = false, encoding?: 'cp1251',
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('formula_policy', quarantineFormulas ? 'quarantine_rows' : 'reject');
    if (sheet) form.append('sheet', sheet);
    if (encoding) form.append('encoding', encoding);
    return call<SupplierSourceInspection>(
      precisionPath(org, supplier) + '/inspect',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } },
      60_000,
    );
  },
  candidateAdjudicationHistory: (org: string, queueSha: string, offset = 0) =>
    call<CandidateAdjudicationHistory>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-adjudications?queue_sha256=' + encodeURIComponent(queueSha) +
      '&offset=' + encodeURIComponent(String(offset)),
    ),
  attestCandidateAdjudication: (
    org: string, archive: File, csrf: string, inspect: CandidateInspectResponse,
    task: CandidateReviewTask, finding: CandidateAdjudicationFinding,
    judgments: Record<string, 'accepted' | 'rejected'>,
    noCandidateMatch: boolean, rationale: string,
  ) => {
    const form = new FormData();
    form.append('evidence', archive);
    form.append('review_json', JSON.stringify({
      archive_sha256: inspect.archive_sha256,
      queue_sha256: inspect.queue_sha256,
      task_sha256: task.task_sha256, request_id: task.request_id, finding,
      accepted_skus: finding === 'resolve_proposals'
        ? task.candidate_skus.filter(s => judgments[s] === 'accepted') : [],
      rejected_skus: finding === 'resolve_proposals'
        ? task.candidate_skus.filter(s => judgments[s] === 'rejected') : [],
      no_candidate_match: finding === 'resolve_proposals' && noCandidateMatch,
      rationale: rationale.trim(), personally_adjudicated: true,
    }));
    return call<{
      recorded: boolean; queue_sha256: string; task_sha256: string;
      request_id: string; finding: CandidateAdjudicationFinding;
      adjudicator_user_id: string; reviewer_independence_verified: boolean;
      rights_verified: false; privacy_review_complete: false;
      independent_gold: false; training_approved: false; benchmark_approved: false;
    }>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-adjudications',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  candidateConsensus: (org: string, queueSha: string) =>
    call<CandidateConsensus>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews/consensus?queue_sha256=' + encodeURIComponent(queueSha),
    ),
  candidateSourceRestrictions: (org: string, queueSha: string, offset = 0) =>
    call<SourceRestrictionHistory>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews/source-restrictions?queue_sha256=' +
      encodeURIComponent(queueSha) + '&offset=' + encodeURIComponent(String(offset)),
    ),
  attestCandidateSourceRestriction: (
    org: string, archive: File, csrf: string, inspect: CandidateInspectResponse,
    sourceSha: string, kind: 'rights' | 'privacy',
    finding: SourceRestrictionFinding, evidenceSha: string | null,
    rationale: string,
  ) => {
    const form = new FormData();
    form.append('evidence', archive);
    form.append('review_json', JSON.stringify({
      archive_sha256: inspect.archive_sha256,
      queue_sha256: inspect.queue_sha256,
      source_sha256: sourceSha, review_kind: kind, finding,
      evidence_sha256: evidenceSha, rationale: rationale.trim(),
      personally_reviewed: true,
    }));
    return call<{
      recorded: boolean; source_sha256: string; review_kind: 'rights' | 'privacy';
      finding: SourceRestrictionFinding; reviewer_user_id: string;
      rights_verified: false; privacy_review_complete: false;
      training_approved: false;
    }>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews/source-restrictions',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  inspectCandidateReview: (
    org: string, archive: File, csrf: string, offset = 0,
  ) => {
    const form = new FormData();
    form.append('evidence', archive);
    form.append('offset', String(offset));
    form.append('limit', '25');
    return call<CandidateInspectResponse>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews/inspect',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  attestCandidateReview: (
    org: string, archive: File, csrf: string,
    inspect: CandidateInspectResponse, task: CandidateReviewTask,
    decisions: Record<string, CandidateJudgment>, rationale: string,
    noCandidateMatch: boolean,
  ) => {
    const form = new FormData();
    form.append('evidence', archive);
    form.append('review_json', JSON.stringify({
      archive_sha256: inspect.archive_sha256,
      queue_sha256: inspect.queue_sha256,
      task_sha256: task.task_sha256,
      request_id: task.request_id,
      accepted_skus: task.candidate_skus.filter(s => decisions[s] === 'accepted'),
      rejected_skus: task.candidate_skus.filter(s => decisions[s] === 'rejected'),
      unresolved_skus: task.candidate_skus.filter(s => decisions[s] === 'unresolved'),
      no_candidate_match: noCandidateMatch,
      rationale: rationale.trim(), personally_reviewed: true,
    }));
    return call<CandidateReviewReceipt>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } }, 60_000,
    );
  },
  candidateReviewHistory: (org: string, queueSha: string, offset = 0) =>
    call<CandidateHistory>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/candidate-reviews?queue_sha256=' + encodeURIComponent(queueSha) +
      '&offset=' + encodeURIComponent(String(offset)),
    ),
  previewSupplierStage: (
    org: string, supplier: string, file: File,
    mapping: SupplierPrecisionMapping, csrf: string,
    start = 0, limit = 50, quarantineFormulas = false,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('mapping_json', JSON.stringify(mapping));
    form.append('formula_policy', quarantineFormulas ? 'quarantine_rows' : 'reject');
    form.append('start', String(start));
    form.append('limit', String(limit));
    return call<SupplierStagePreview>(
      precisionPath(org, supplier) + '/stage',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } },
      60_000,
    );
  },
  attestSupplierUnit: (
    org: string, supplier: string, file: File,
    mapping: SupplierPrecisionMapping, sourceSha: string,
    decision: SupplierUnitDecision, csrf: string, quarantineFormulas = false,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      source_sha256: sourceSha,
      source_row_sha256: decision.source_row_sha256,
      line: decision.line, mapping,
      formula_policy: quarantineFormulas ? 'quarantine_rows' : 'reject',
      finding: decision.finding, approved_sale_unit: decision.approved_sale_unit,
      approved_pack_size: decision.approved_pack_size,
      rationale: decision.rationale, personally_reviewed: true,
    }));
    return call<{
      recorded: true; source_sha256: string; source_row_sha256: string; line: number;
      published: false; model_training_authorized: false;
    }>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/suppliers/' + encodeURIComponent(supplier) + '/unit-reviews',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } },
      60_000,
    );
  },
  supplierUnitHistory: (
    org: string, supplier: string, sourceSha: string, offset = 0,
  ) => call<SupplierUnitHistory>(
    '/api/session/organizations/' + encodeURIComponent(org) +
    '/suppliers/' + encodeURIComponent(supplier) + '/unit-reviews' +
    '?source_sha256=' + encodeURIComponent(sourceSha) +
    '&offset=' + encodeURIComponent(String(offset)),
  ),
  previewSupplierPrecision: (
    org: string, supplier: string, file: File,
    mapping: SupplierPrecisionMapping, csrf: string,
    start = 0, limit = 50, quarantineFormulas = false,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('mapping_json', JSON.stringify(mapping));
    form.append('formula_policy', quarantineFormulas ? 'quarantine_rows' : 'reject');
    form.append('start', String(start));
    form.append('limit', String(limit));
    return call<SupplierPrecisionPreview>(
      precisionPath(org, supplier) + '/preview',
      { method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf } },
      60_000,
    );
  },
  attestSupplierPrecision: (
    org: string, supplier: string, file: File,
    mapping: SupplierPrecisionMapping, preview: SupplierPrecisionPreview,
    decision: SupplierPrecisionDecision, csrf: string, quarantineFormulas = false,
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('review_json', JSON.stringify({
      source_sha256: preview.source_sha256,
      source_row_sha256: decision.source_row_sha256,
      mapping, formula_policy: quarantineFormulas ? 'quarantine_rows' : 'reject',
      line: decision.line, finding: decision.finding,
      rounding_mode: decision.rounding_mode,
      rounded_price_rub: decision.rounded_price_rub,
      rationale: decision.rationale,
    }));
    return call<{
      source_sha256: string; source_row_sha256: string; line: number;
      recorded: true; published: false; current_price_verified: false;
    }>(precisionPath(org, supplier), {
      method: 'POST', body: form, headers: { 'X-CSRF-Token': csrf },
    }, 60_000);
  },
  supplierPrecisionHistory: (
    org: string, supplier: string, sourceSha: string, line: number,
  ) => call<SupplierPrecisionHistory>(
    precisionPath(org, supplier) +
    '?source_sha256=' + encodeURIComponent(sourceSha) +
    '&line=' + encodeURIComponent(String(line)),
  ),
  invitations: () =>
    call<TenantInvitation[]>('/api/session/invitations'),
  inviteByEmail: (org: string, email: string, role: 'viewer' | 'editor',
                  csrf: string) =>
    call<{ status: 'accepted' }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/invitations/email',
      { ...json({ email, role }), headers: { 'X-CSRF-Token': csrf } },
    ),
  joinInvitation: (token: string, password: string) =>
    call<{ user_id: string; organization_id: string }>(
      '/api/session/invitations/register', json({ token, password }),
    ),
  invite: (org: string, email: string, role: 'viewer' | 'editor', csrf: string) =>
    call<{ invitation_id: string }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/invitations',
      { ...json({ email, role }), headers: { 'X-CSRF-Token': csrf } },
    ),
  acceptInvite: (invitationId: string, csrf: string) =>
    call<null>('/api/session/invitations/' + encodeURIComponent(invitationId) + '/accept',
      { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  members: (org: string) => call<TenantMember[]>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/members',
  ),
  changeRole: (org: string, user: string, role: 'viewer' | 'editor', csrf: string) =>
    call<null>(
      '/api/session/organizations/' + encodeURIComponent(org) +
        '/members/' + encodeURIComponent(user),
      { method: 'PATCH', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ role }) },
    ),
  revokeMember: (org: string, user: string, csrf: string) =>
    call<null>(
      '/api/session/organizations/' + encodeURIComponent(org) +
        '/members/' + encodeURIComponent(user),
      { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } },
    ),
  extractRequest: (
    org: string, sourceText: string, csrf: string, consent: boolean,
  ) => callLlm<TenantExtractionPreview>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/requests/extract',
    { text: sourceText, consent_to_external_processing: consent }, csrf,
  ),
  analyzeRequest: (org: string, lines: QuoteLineInput[], csrf: string) => call<RequestAnalysis>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/requests/analyze',
    { ...json({ extracted_request: { lines } }), headers: { 'X-CSRF-Token': csrf } },
  ),
  suggestCategory: (org: string, description: string, original: string | null, csrf: string) =>
    call<TenantCategorySuggestion>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/matches/category-suggestion',
      { ...json({ description, original_category: original }), headers: { 'X-CSRF-Token': csrf } },
    ),
  previewMatches: (
    org: string, catalogId: string, requestedItem: object, csrf: string,
    instructions?: ConfirmedInstructions, mode: 'ml' | 'rapidfuzz' | 'tfidf' = 'ml',
  ) =>
    call<TenantMatchResponse>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/matches/preview',
      {
        ...json({ catalog_version_id: catalogId, requested_item: requestedItem, limit: 20,
          instructions, mode }),
        headers: { 'X-CSRF-Token': csrf },
      },
    ),
  previewBatchMatches: (
    org: string, catalogId: string, requestedItems: object[], csrf: string,
    instructions?: ConfirmedInstructions,
  ) => call<TenantBatchMatchResponse>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/matches/batch-preview',
    {
      ...json({ catalog_version_id: catalogId, requested_items: requestedItems,
        limit: 20, instructions, mode: 'tfidf' }),
      headers: { 'X-CSRF-Token': csrf },
    },
  ),
  previewVariants: (
    org: string, catalogId: string, requestedItems: object[], csrf: string,
    instructions?: ConfirmedInstructions,
  ) => call<TenantVariantsResponse>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/matches/variants',
    { ...json({ catalog_version_id: catalogId, requested_items: requestedItems,
      limit: 20, instructions, mode: 'tfidf' }), headers: { 'X-CSRF-Token': csrf } },
  ),
  previewQuoteInstructions: (org: string, text: string, csrf: string) =>
    call<InstructionProposal>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/instructions/preview',
      { ...json({ text }), headers: { 'X-CSRF-Token': csrf } },
    ),
  listQuotes: (org: string, cursor?: string) => call<QuoteListPage>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes'
      + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''),
  ),
  trainingFeedbackContext: (org: string, quote: TenantQuote, csrf: string) => call<TrainingFeedbackContext>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id)
      + '/training-feedback/preview',
    { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256 }),
      headers: { 'X-CSRF-Token': csrf } },
  ),
  exportTrainingFeedback: (org: string, quote: TenantQuote, decisions: TrainingFeedbackDecision[],
                          reviewed: boolean, trainingConsent: boolean, csrf: string) => call<TrainingFeedbackBundle>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id)
      + '/training-feedback',
    { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256,
      decisions, reviewed, training_consent: trainingConsent }), headers: { 'X-CSRF-Token': csrf } },
  ),
  saveTrainingFeedback: (org: string, quote: TenantQuote, decisions: TrainingFeedbackDecision[],
                        reviewed: boolean, trainingConsent: boolean, csrf: string, key: string) => call<TrainingFeedbackSummary>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id)
      + '/training-feedback/history',
    { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256,
      decisions, reviewed, training_consent: trainingConsent }), headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key } },
  ),
  trainingFeedbackHistory: (org: string, quoteId: string, csrf: string, before?: string) => call<TrainingFeedbackHistory>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quoteId)
      + '/training-feedback/history' + (before ? '?before=' + encodeURIComponent(before) : ''),
    { headers: { 'X-CSRF-Token': csrf } },
  ),
  downloadSavedTrainingFeedback: (org: string, quoteId: string, feedbackId: string, csrf: string) => call<TrainingFeedbackBundle>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quoteId)
      + '/training-feedback/history/' + encodeURIComponent(feedbackId), { headers: { 'X-CSRF-Token': csrf } },
  ),
  withdrawTrainingFeedback: (org: string, quoteId: string, feedbackId: string, csrf: string) => call<TrainingFeedbackSummary>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quoteId)
      + '/training-feedback/history/' + encodeURIComponent(feedbackId) + '/withdraw',
    { ...json({}), headers: { 'X-CSRF-Token': csrf } },
  ),
  getQuote: (org: string, quoteId: string, revision?: number) => call<TenantQuote>(
    '/api/session/organizations/' + encodeURIComponent(org) +
      '/quotes/' + encodeURIComponent(quoteId) +
      (revision === undefined ? '' : '?revision=' + encodeURIComponent(revision)),
  ),
  createQuote: (org: string, catalogId: string, lines: QuoteLineInput[],
                csrf: string, key: string, review?: ReviewEvidence,
                selections?: ManagerSkuSelection[],
                instructions?: ConfirmedInstructions) => call<TenantQuote>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes',
    {
      ...json({
        catalog_version_id: catalogId, extracted_request: { lines }, review,
        manager_selections: selections, instructions,
      }),
      headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
    },
  ),
  reviseQuote: (org: string, quoteId: string, revision: number,
                catalogId: string, lines: QuoteLineInput[], csrf: string,
                key: string, review?: ReviewEvidence,
                selections?: ManagerSkuSelection[],
                instructions?: ConfirmedInstructions) =>
    call<TenantQuote>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' +
        encodeURIComponent(quoteId) + '/revisions',
      {
        ...json({
          catalog_version_id: catalogId, extracted_request: { lines },
          expected_revision: revision, review, manager_selections: selections,
          instructions,
        }),
        headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
      },
    ),
  managerDecisions: (org: string, quoteId: string, revision: number) =>
    call<ManagerDecisionHistory>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/quotes/' + encodeURIComponent(quoteId) +
      '/manager-decisions/' + revision,
    ),
  quoteReview: (org: string, quoteId: string, revision: number) =>
    call<QuoteReviewAudit>(
      '/api/session/organizations/' + encodeURIComponent(org) +
      '/quotes/' + encodeURIComponent(quoteId) + '/reviews/' + revision,
    ),
  companyProfile: (org: string) => call<{ profile: CompanyProfileSnapshot | null }>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/company-profile',
  ),
  prepareCompanyLogo: (org: string, file: File, csrf: string) => {
    const body = new FormData(); body.append('file', file);
    return call<{ logo: CompanyLogo }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/company-profile/logo/prepare',
      { method: 'POST', body, headers: { 'X-CSRF-Token': csrf } },
    );
  },
  saveCompanyProfile: (org: string, data: CompanyProfileData, expected: number, csrf: string) =>
    call<{ profile: CompanyProfileSnapshot }>(
      '/api/session/organizations/' + encodeURIComponent(org) + '/company-profile',
      { ...json({ data, expected_revision: expected, reviewed: true }), method: 'PUT',
        headers: { 'X-CSRF-Token': csrf } },
    ),
  lookupCompany: (org: string, inn: string, csrf: string) => call<PartyLookupResponse>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/company-lookup',
    { ...json({ inn }), headers: { 'X-CSRF-Token': csrf } },
  ),
  previewDocumentAlternatives: (org: string, quote: TenantQuote, csrf: string) => call<DocumentAlternativePreview>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id) + '/alternatives',
    { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256 }),
      headers: { 'X-CSRF-Token': csrf } },
  ),
  generatePresentation: (
    org: string, quote: TenantQuote, context: ProposalContext, csrf: string,
    consent: boolean, companyProfileRevision?: number, alternativeSelections?: AlternativeSelection[],
  ) => callLlm<PresentationPreview>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/'
      + encodeURIComponent(quote.quote_id) + '/presentation', {
      ...context, expected_revision: quote.revision,
        source_snapshot_sha256: quote.snapshot_sha256,
        consent_to_external_processing: consent, company_profile_revision: companyProfileRevision,
        alternative_selections: alternativeSelections,
    }, csrf,
  ),
  previewDocumentPdf: (org: string, quote: TenantQuote, document: CommercialDocument, csrf: string) => call<Blob>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/' + encodeURIComponent(quote.quote_id) + '/document-preview',
    { ...json({ expected_revision: quote.revision, source_snapshot_sha256: quote.snapshot_sha256, document }),
      headers: { 'X-CSRF-Token': csrf } }, 35_000, true,
  ),
  approveDocument: (
    org: string, quote: TenantQuote, document: CommercialDocument,
    csrf: string, key: string,
  ) => call<TenantQuote>(
    '/api/session/organizations/' + encodeURIComponent(org) + '/quotes/'
      + encodeURIComponent(quote.quote_id) + '/approve', {
      ...json({ expected_revision: quote.revision,
        source_snapshot_sha256: quote.snapshot_sha256, document, reviewed: true }),
      headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': key },
    },
  ),
  quotePdfPath: (org: string, quoteId: string, revision: number) =>
    '/api/session/organizations/' + encodeURIComponent(org) +
      '/quotes/' + encodeURIComponent(quoteId) + '/pdf?revision=' +
      encodeURIComponent(revision),
  logout: (csrf: string) =>
    call<null>('/api/session/logout', {
      method: 'POST', headers: { 'X-CSRF-Token': csrf },
    }),
};
