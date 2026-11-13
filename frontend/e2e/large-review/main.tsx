/** Isolated A91 React browser fixture. Excluded from production build. */
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { SupplierCheckpointProgress } from '../../src/components/SupplierCheckpointProgress';
import type { ActiveTenantCatalog, SupplierCanonicalDecision, SupplierCanonicalReview, SupplierPrecisionMapping, SupplierStagePreview } from '../../src/tenant-session';

type Fixture = {
  organization_id: string; supplier_id: string; source: string;
  source_sha256: string; mapping: SupplierPrecisionMapping;
  decisions: SupplierCanonicalDecision[];
};
const response = await fetch('/__e2e/fixture', { credentials: 'same-origin', cache: 'no-store' });
if (!response.ok) throw new Error('Isolated A91 source fixture not available');
const fixture = await response.json() as Fixture;
const auth = await fetch('/api/session/resume', { credentials: 'same-origin', cache: 'no-store' });
if (!auth.ok) throw new Error('A91 browser session unavailable');
const session = await auth.json() as { csrf_token: string };
if (!/^[0-9a-f]{64}$/.test(fixture.source_sha256)
  || !Array.isArray(fixture.decisions) || fixture.decisions.length !== 121
  || new Set(fixture.decisions.map(item => item.line)).size !== 121
  || !session.csrf_token) throw new Error('Invalid isolated fixture');
const source = new File([fixture.source], 'paging.csv', { type: 'text/csv' });
const review: SupplierCanonicalReview = {
  source_sha256: fixture.source_sha256, mapping: fixture.mapping,
  price_basis: 'standard', formula_policy: 'reject',
  explicitly_confirmed_price_basis: true, decisions: [],
};
const BATCH = 45;
function TestReview() {
  const [page, setPage] = useState(0);
  const [published, setPublished] = useState<ActiveTenantCatalog | null>(null);
  const [reset, setReset] = useState(0);
  const [recovered, setRecovered] = useState(0);
  const decisions = fixture.decisions.slice(page * BATCH, (page + 1) * BATCH);
  const visible: Record<number, SupplierCanonicalDecision> =
    Object.fromEntries(decisions.map(item => [item.line, item]));
  const stage = {
    source_sha256: fixture.source_sha256, total_rows: fixture.decisions.length,
    sampled_rows: decisions.map(item => ({
      line: item.line, source_row_sha256: item.source_row_sha256,
    })),
  } as SupplierStagePreview;
  return <main>
    <h1>A91 isolated React checkpoint acceptance</h1>
    <p data-testid="page">Batch {page + 1} / 3</p>
    <button type="button" onClick={() => setPage(current => Math.min(2, current + 1))}>Next reviewed source page</button>
    <button type="button" onClick={() => setReset(current => current + 1)}>Remount review component</button>
    <p data-testid="restored">Restored rows: {recovered}</p>
    <SupplierCheckpointProgress key={reset}
      organizationId={fixture.organization_id} supplierId={fixture.supplier_id}
      source={source} csrf={session.csrf_token} review={review}
      stage={stage} reviews={visible} busy={false}
      priceBasisConfirmed={true}
      onRestored={rows => setRecovered(Object.keys(rows).length)}
      onPublished={value => setPublished(value)}/>
    {published && <output data-testid="published">{JSON.stringify(published)}</output>}
  </main>;
}
const root = document.getElementById('root');
if (!root) throw new Error('A91 isolated React root missing');
createRoot(root).render(<TestReview/>);
