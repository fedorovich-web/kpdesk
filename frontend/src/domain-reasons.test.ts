import { describe, expect, it } from 'vitest';
import { readableReason } from './domain';

describe('matching explanation fallback and source distinctions', () => {
  it.each(['future_reason', 'constructor', 'toString', '__proto__'])('handles unknown %s as text', code => {
    expect(readableReason(code)).toBe('Требуется проверка соответствия');
  });

  it('distinguishes a missing product fact from an unspecified request and a mismatch', () => {
    const product = readableReason('paper_gsm_unknown');
    const request = readableReason('paper_gsm_request_unknown');
    const mismatch = readableReason('paper_gsm_mismatch');
    expect(product).toContain('у товара');
    expect(request).toContain('В заявке');
    expect(mismatch).toContain('не соответствует требованиям');
    expect(new Set([product, request, mismatch]).size).toBe(3);
    expect([product, request, mismatch].join(' ')).not.toContain('paper_gsm');
  });
});
