import { useEffect, useRef, useState } from 'react';
import { categoryLabel, categories } from '../categories';
import type { AttrSpec } from '../categories';
import type { AttributeValue, CategorySuggestion, ExtractedLine, SaleUnit } from '../domain';

function attr(raw: string, field: AttrSpec): AttributeValue {
  const text = raw.trim();
  if (!text) return null;
  if (field.type === 'number') {
    const number = Number(text);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }
  if (field.type === 'list') {
    const result = [...new Set(text.split(/[;\n]/).map((item) => item.trim()).filter(Boolean))];
    return result.length ? result : null;
  }
  return text;
}

interface Props {
  isDemo?: boolean;
  line: ExtractedLine;
  index: number;
  onChange: (line: ExtractedLine) => void;
  onSuggestCategory?: (description: string, original: string | null) => Promise<CategorySuggestion>;
  onRemove: () => void;
}

export function LineEditor({ line, index, onChange, onRemove, onSuggestCategory, isDemo = false }: Props) {
  const [suggestion, setSuggestion] = useState<CategorySuggestion | null>(null);
  const [suggestionError, setSuggestionError] = useState<string | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const requestVersion = useRef(0);
  useEffect(() => {
    requestVersion.current += 1;
    setSuggestion(null);
    setSuggestionError(null);
    setSuggesting(false);
  }, [line.description, line.category]);
  const checkCategory = async () => {
    if (!onSuggestCategory || !line.description.trim() || suggesting) return;
    const version = ++requestVersion.current;
    setSuggesting(true);
    setSuggestion(null);
    setSuggestionError(null);
    try {
      const result = await onSuggestCategory(line.description, line.category);
      if (version === requestVersion.current) setSuggestion(result);
    } catch (error) {
      if (version === requestVersion.current) {
        setSuggestionError(error instanceof Error ? error.message : 'Не удалось проверить категорию.');
      }
    } finally {
      if (version === requestVersion.current) setSuggesting(false);
    }
  };
  const category = categories.find((item) => item.id === line.category);
  const set = <K extends keyof ExtractedLine>(key: K, value: ExtractedLine[K]) =>
    onChange({ ...line, [key]: value });
  const setCategory = (value: string) =>
    onChange({ ...line, category: value || null, attributes: {},
      paper_format: null, paper_gsm: null, pen_ink_color: null, pen_ink_type: null });
  return (
    <section className="line-editor" aria-label={'Позиция ' + (index + 1)}>
      <div className="line-editor-head">
        <span className="step-dot small">{index + 1}</span>
        <strong>{categoryLabel(line.category)}</strong>
        <span className="line-caption">Проверьте характеристики</span>
        <button className="text-button danger" type="button" onClick={onRemove}>Удалить</button>
      </div>
      <div className="fields-grid">
        <label className="field field-span">Наименование
          <input required maxLength={500} value={line.description}
            onChange={(e) => set('description', e.target.value)} />
        </label>
        <label className="field field-span">Категория
          <select value={line.category ?? ''} onChange={(e) => setCategory(e.target.value)}>
            <option value="">Не определена — требуется проверка</option>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
        </label>
        {onSuggestCategory && <div className="field field-span">
          <button className="button button-white" type="button"
            disabled={suggesting || !line.description.trim()} onClick={() => void checkCategory()}>
            {suggesting ? 'Проверка категории…' : 'Предложить категорию'}
          </button>
          {suggestionError && <p role="alert">{suggestionError}</p>}
          {suggestion && <div role="status">
            {suggestion.abstained || !suggestion.suggested_category
              ? <p>Не удалось однозначно определить категорию. Выберите её вручную.</p>
              : <p>Предлагаемая категория: {categoryLabel(suggestion.suggested_category)}.
                  Оценка: {Math.round(suggestion.confidence * 100)}%.
                  Разница с ближайшей категорией: {Math.round(suggestion.margin * 100)} п.п.
                  Проверьте категорию по заявке перед подтверждением.</p>}
            {suggestion.suggested_category && suggestion.suggested_category !== line.category &&
              <button type="button" className="button button-white"
                onClick={() => { if (suggestion.suggested_category) setCategory(suggestion.suggested_category); }}>
                Подтвердить смену категории
              </button>}
          </div>}
        </div>}
        <label className="field" data-demo-target={isDemo && index === 0 ? 'quantity' : undefined}>Количество
          <input type="number" min="1" max="100000" step="1" placeholder="Не указано"
            value={line.quantity ?? ''} onChange={(e) => {
              const n = Number(e.target.value);
              set('quantity', e.target.value && Number.isInteger(n) && n > 0 ? n : null);
            }} />
        </label>
        <label className="field">Единица
          <select value={line.unit ?? ''} onChange={(e) => set('unit', (e.target.value || null) as SaleUnit | null)}>
            <option value="">Не указана</option>
            {(['штука', 'пачка', 'коробка'] as const).map((v) => <option key={v}>{v}</option>)}
          </select>
        </label>
        <label className="field">Бренд
          <input value={line.brand ?? ''} placeholder="Не указан" maxLength={150}
            onChange={(e) => set('brand', e.target.value || null)} />
        </label>
        {line.category === 'paper' && <>
          <label className="field">Формат бумаги
            <input value={line.paper_format ?? ''} onChange={(e) => set('paper_format', e.target.value || null)} />
          </label>
          <label className="field">Плотность, г/м²
            <input type="number" min="1" max="1000" value={line.paper_gsm ?? ''}
              onChange={(e) => set('paper_gsm', e.target.value ? Number(e.target.value) : null)} />
          </label>
        </>}
        {line.category === 'pen' && <>
          <label className="field">Цвет чернил
            <input value={line.pen_ink_color ?? ''} onChange={(e) => set('pen_ink_color', e.target.value || null)} />
          </label>
          <label className="field">Тип чернил
            <input value={line.pen_ink_type ?? ''} onChange={(e) => set('pen_ink_type', e.target.value || null)} />
          </label>
        </>}
        {category?.fields.map((field) => {
          const val = line.attributes[field.key];
          const displayed = Array.isArray(val) ? val.join('; ') : String(val ?? '');
          return <label className="field" key={field.key}>{field.label}
            <input type={field.type === 'number' ? 'number' : 'text'}
              min={field.type === 'number' ? 1 : undefined}
              placeholder={field.placeholder ?? (field.type === 'list' ? 'Разделяйте точкой с запятой' : 'Не указано')}
              value={displayed}
              onChange={(e) => onChange({
                ...line, attributes: { ...line.attributes, [field.key]: attr(e.target.value, field) },
              })} />
          </label>;
        })}
      </div>
      {line.source_quote && <p className="source-quote">Из заявки: <q>{line.source_quote}</q></p>}
    </section>
  );
}
