import { describe, expect, it } from 'vitest';
import { splitRequestForManualReview } from './manual-request-split';

describe('A48 safe deterministic manual request splitter', () => {
  it('preserves source evidence without guessing quantities or splitting semicolons', () => {
    expect(splitRequestForManualReview(
      ' 20 пачек бумаги A4; 80 г/м²\r\n\n   Коврик для мыши  \n',
    )).toEqual(['20 пачек бумаги A4; 80 г/м²', 'Коврик для мыши']);
  });

  it('rejects empty, oversized, and too-many lines rather than discarding data', () => {
    expect(() => splitRequestForManualReview(' \n\r\n ')).toThrow(/Введите текст заявки/);
    expect(() => splitRequestForManualReview('x'.repeat(501))).toThrow(/500/);
    expect(() => splitRequestForManualReview(
      Array.from({ length: 21 }, (_, i) => 'item ' + i).join('\n'),
    )).toThrow(/20/);
  });

  it('never interprets SKU-like digits or technical specifications', () => {
    expect(splitRequestForManualReview('Картридж 120XL A4 80 г/м²')).toEqual([
      'Картридж 120XL A4 80 г/м²',
    ]);
  });
});
