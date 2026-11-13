/** Safe fallback: preserve source lines for manual entry, infer no attributes. */
export function splitRequestForManualReview(source: string): string[] {
  const lines = source.split(/\r?\n/u).map(line => line.trim()).filter(Boolean);
  if (lines.length === 0) {
    throw new Error('Введите текст заявки.');
  }
  if (lines.length > 20) {
    throw new Error('В одной заявке может быть до 20 строк. Разделите текст на несколько заявок.');
  }
  if (lines.some(line => line.length > 500)) {
    throw new Error('В строке может быть до 500 символов. Разделите длинную строку.');
  }
  return lines;
}
