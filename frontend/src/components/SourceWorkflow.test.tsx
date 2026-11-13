// @vitest-environment-options {"url":"https://localhost:5173"}
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SupplierPrecisionReview } from './SupplierPrecisionReview';
import { SupplierCatalogChanges } from './SupplierCatalogChanges';
import { SupplierQuoteComparison } from './SupplierQuoteComparison';

afterEach(() => vi.unstubAllGlobals());

describe('price sources without a counterparty registry', () => {
  it('opens review, version history and comparison without a registry request', () => {
    const fetch = vi.fn().mockRejectedValue(new Error('Network must remain idle'));
    vi.stubGlobal('fetch', fetch);
    render(<>
      <SupplierPrecisionReview organizationId="org" csrf="csrf"/>
      <SupplierCatalogChanges organizationId="org" csrf="csrf"/>
      <SupplierQuoteComparison organizationId="org" csrf="csrf"
        activeVersionId="catalog" requestLines={[]} onSaved={vi.fn()}/>
    </>);
    expect(screen.getByLabelText('Идентификатор поставщика')).toBeEnabled();
    expect(screen.queryByLabelText('Поставщик из реестра')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
});
