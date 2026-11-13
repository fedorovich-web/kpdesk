import { describe, expect, it } from 'vitest';
import { demoProposalExample } from './narrative';
import type { AutoDraft, ExtractedRequest } from '../domain';

const extracted: ExtractedRequest = { lines: [{
  source_quote: '2 пачки бумаги', description: 'Бумага офисная А4 80 г/м2 500 листов',
  category: 'paper', quantity: 2, unit: 'пачка', brand: null, paper_format: 'A4',
  paper_gsm: 80, pen_ink_color: null, pen_ink_type: null, attributes: {},
}] };
const draft: AutoDraft = { catalog_version_id: 'demo-synthetic-catalog-v1',
  approval_status: 'not_approved', ready_for_draft: true, total_rub: '840.00', lines: [{
    line_number: 1, status: 'auto_selected', sku: 'PAPER-A4-80', line_total_rub: '840.00',
    reason_codes: [], selection_method: 'exact_demo_fixture_match',
  }] };

describe('authored synthetic proposal example', () => {
  it('uses only the currently matched fixture rows and quantities', () => {
    const example = demoProposalExample(draft, extracted);
    expect(example).not.toBeNull();
    expect(example?.composition).toContain('2 пачки');
    expect(example?.composition).not.toContain('ручк');
    expect(demoProposalExample(draft, { lines: [{ ...extracted.lines[0]!, quantity: 4 }] })?.composition)
      .toContain('4 пачки');
  });

  it('does not paste an example over unsupported, unresolved or foreign catalog rows', () => {
    expect(demoProposalExample({ ...draft, ready_for_draft: false }, extracted)).toBeNull();
    expect(demoProposalExample({ ...draft, catalog_version_id: 'real' }, extracted)).toBeNull();
    expect(demoProposalExample({ ...draft, lines: [{ ...draft.lines[0]!, sku: 'OTHER' }] }, extracted)).toBeNull();
  });
});
