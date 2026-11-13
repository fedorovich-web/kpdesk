import type { FormEvent } from 'react';
import { ArrowLeft, ArrowRight, FileText, LoaderCircle, Plus, Sparkles, UploadCloud } from 'lucide-react';
import type { Catalog, CategorySuggestion, ExtractedLine, ExtractedRequest, InstructionProposal } from '../domain';
import { money } from '../domain';
import { MODELS } from '../domain';
import { designAssets } from '../design-assets';
import { LineEditor } from './LineEditor';
import { WorkflowSteps } from './WorkflowSteps';

export interface RequestPageProps {
  isDemo?: boolean;
  catalog: Catalog | null;
  catalogError: string | null;
  requestText: string;
  model: string;
  extracted: ExtractedRequest | null;
  rulesText: string;
  rulesProposal: InstructionProposal | null;
  rulesConfirmed: boolean;
  reviewingRules: boolean;
  onRulesTextChange: (text: string) => void;
  onPreviewRules: () => void;
  onConfirmRules: () => void;
  busy: boolean;
  extracting: boolean;
  drafting: boolean;
  onTextChange: (text: string) => void;
  onModelChange: (model: string) => void;
  onExtract: (event: FormEvent<HTMLFormElement>) => void;
  onAddManual: () => void;
  onAddLine: () => void;
  onChangeLine: (index: number, line: ExtractedLine) => void;
  onSuggestCategory?: (description: string, original: string | null) => Promise<CategorySuggestion>;
  onRemoveLine: (index: number) => void;
  onResetLines: () => void;
  onDraft: () => void;
  onBack: () => void;
  onImport: () => void;
}

export function RequestPage({
  catalog, catalogError, requestText, model, extracted, busy, extracting, drafting, isDemo = false,
  rulesText, rulesProposal, rulesConfirmed, reviewingRules,
  onRulesTextChange, onPreviewRules, onConfirmRules,
  onTextChange, onModelChange, onExtract, onAddManual, onAddLine, onChangeLine,
  onRemoveLine, onResetLines, onDraft, onBack, onImport, onSuggestCategory,
}: RequestPageProps) {
  const itemCount = extracted?.lines.length ?? 0;
  const incomplete = extracted?.lines.some(line => !line.description.trim()) ?? false;
  const canDraft = !!catalog && itemCount > 0 && !incomplete && !busy
    && (!rulesText.trim() || rulesConfirmed);

  return <div className="request-page">
    <div className="request-intro">
      <h1>Заявка клиента</h1>
      <p>Вставьте заявку клиента или добавьте товары вручную. Проверьте количество и характеристики, затем подберите товары из прайса.</p>
      <WorkflowSteps active={extracted ? 2 : 1}/>
    </div>

    <div className="request-grid">
      <div className="request-main">
        <section className="panel request-source-panel" aria-labelledby="request-source-title" data-demo-target={isDemo ? 'source' : undefined}>
          <div className="panel-heading">
            <h2 id="request-source-title">Добавьте заявку</h2>
              <p>{isDemo ? 'Используйте готовую учебную заявку или добавьте товары вручную. Распознавание вашего текста в демо недоступно.' : 'Вставьте письмо клиента, список товаров или спецификацию. После распознавания проверьте названия, количество и характеристики.'}</p>
          </div>
          <form id="request-source-form" onSubmit={onExtract}>
            <label className="field text-request">
              <span className="request-field-label">Текст заявки <span className="request-required" aria-hidden="true">*</span></span>
              <textarea value={requestText} maxLength={12000} minLength={1} required
                disabled={busy}
                onChange={event => onTextChange(event.target.value)}
                placeholder="Например: Нужно 10 карандашей графитовых HB и 2 папки для документов А4 на молнии."
                rows={6}/>
              <small className="request-counter">{requestText.length.toLocaleString('ru-RU')} / 12 000 символов</small>
            </label>
            {!isDemo && <div className="request-model-row">
              <label className="field model-select">Модель ИИ для распознавания
                <select value={model} disabled={busy} onChange={event => onModelChange(event.target.value)}>
                  {MODELS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <p>ИИ выделит товары из текста. Цены будут взяты из вашего прайса при расчёте.</p>
            </div>}
            {isDemo && <p>Демо открывает готовую учебную заявку без обращения к ИИ. Свою заявку можно заполнить вручную; распознавание доступно в личном кабинете.</p>}
          </form>
        </section>

        <section className="panel request-source-panel" aria-labelledby="request-rules-title" data-demo-target={isDemo ? 'rules' : undefined}>
          <div className="panel-heading">
            <h2 id="request-rules-title">Наценка, бюджет и бренды</h2>
            <p>Поддерживаются наценка, максимальный бюджет, обязательный бренд и исключение брендов.
              Проверьте и подтвердите условия. Если условие не распознано, уточните его перед расчётом.</p>
          </div>
          <label className="field text-request">
            <span className="request-field-label">Условия подбора и расчёта</span>
            <textarea value={rulesText} maxLength={2000} disabled={busy}
              onChange={event => onRulesTextChange(event.target.value)}
              placeholder="Наценка 18%. Бюджет до 250000 рублей. Исключи бренд Acme."
              rows={3}/>
          </label>
          {rulesText.trim() && <div className="request-editor-actions">
            <button type="button" className="button button-white" disabled={busy}
              onClick={onPreviewRules}>
              {reviewingRules ? 'Проверка…' : 'Проверить условия'}
            </button>
          </div>}
          {rulesProposal && <div role="status" className="request-summary">
            <p><strong>Результат проверки:</strong> наценка {rulesProposal.rules.markup_percent}%,
              {rulesProposal.rules.budget_max_rub
                ? ' максимальная сумма ' + money(rulesProposal.rules.budget_max_rub)
                : ' ограничение бюджета не задано'}.
              {rulesProposal.rules.required_brand
                ? ' Только бренд: ' + rulesProposal.rules.required_brand + '.' : ''}
              {rulesProposal.rules.excluded_brands?.length
                ? ' Исключены: ' + rulesProposal.rules.excluded_brands.join(', ') + '.' : ''}</p>
            {rulesProposal.unresolved_requirements.length > 0 &&
              <p>Нужно уточнить: {rulesProposal.unresolved_requirements.join('; ')}.
                Уточните условия и повторите проверку.</p>}
            {rulesConfirmed ? <p><strong>Условия подтверждены.</strong> {
              isDemo ? 'Они будут применены к учебным данным в браузере.'
                : 'Они будут учтены при расчёте стоимости.'
            }</p>
              : rulesProposal.can_confirm && <button type="button" className="button button-primary"
                disabled={busy} onClick={onConfirmRules}>Подтвердить условия</button>}
          </div>}
          {!rulesText.trim() && <p>Если не указать наценку, для расчёта используются цены из прайса.</p>}
        </section>

        <section className="panel request-products-panel" aria-labelledby="request-products-title" data-demo-target={isDemo ? 'products' : undefined}>
          <div className="panel-heading panel-heading-actions">
            <div>
              <h2 id="request-products-title">Товары из заявки</h2>
              <p>{extracted
                ? 'Сверьте названия, количество и характеристики с заявкой клиента.'
                : 'Распознайте заявку или добавьте товары вручную.'}</p>
            </div>
            {extracted && <span className="request-count">{itemCount} поз.</span>}
          </div>
          {extracted ? <>
            {itemCount ? <div className="editors">
              {extracted.lines.map((line, index) =>
                <LineEditor key={index + ':' + line.source_quote} isDemo={isDemo}
                  line={line} index={index}
                  onChange={updated => onChangeLine(index, updated)}
                  onSuggestCategory={onSuggestCategory}
                  onRemove={() => onRemoveLine(index)}/>)}
            </div> : <p className="request-empty-description">Товаров пока нет. Добавьте первую позицию.</p>}
            <div className="request-editor-actions">
              <button type="button" className="button button-white" disabled={busy || itemCount >= 20}
                onClick={onAddLine}><Plus size={17} aria-hidden="true"/> Добавить позицию</button>
              <button type="button" className="text-button" disabled={busy}
                onClick={onResetLines}>Вернуться к распознаванию</button>
            </div>
          </> : <div className="request-products-empty">
            <img src={designAssets.icon.createQuote} alt="" width={58} height={58} loading="lazy"/>
            <p>В заявке может быть до 20 позиций. Если характеристики не указаны, их нужно уточнить перед подбором.</p>
            <button className="button button-white" type="button" disabled={busy}
              onClick={onAddManual}><Plus size={17} aria-hidden="true"/> Добавить товары вручную</button>
          </div>}
        </section>
      </div>

      <aside className="request-side">
        <section className="panel side-info request-summary" aria-labelledby="request-summary-title">
          <div className="request-summary-header">
            <img src={designAssets.icon.documentInfo} alt="" width={40} height={40} loading="lazy"/>
            <div><h2 id="request-summary-title">Информация о заявке</h2>
              <p>{isDemo ? 'Учебная заявка в этой вкладке' : 'Заявка пока не сохранена'}</p></div>
          </div>
          <dl>
            <div><dt>Источник</dt><dd>{requestText.trim() ? 'Текст заявки' : 'Не указан'}</dd></div>
            <div><dt>Позиции</dt><dd>{extracted ? itemCount : 'Не распознаны'}</dd></div>
            <div><dt>Прайс-лист</dt><dd>{catalog ? isDemo ? 'Учебный · в этой вкладке' : 'Опубликован' : catalogError ? 'Недоступен' : 'Не загружен'}</dd></div>
            <div><dt>Товаров в прайсе</dt><dd>{catalog ? catalog.rows_count : '—'}</dd></div>
            <div><dt>Модель</dt><dd>{isDemo ? 'Не используется в демо' : MODELS.find(item => item.value === model)?.label ?? 'Не определена'}</dd></div>
          </dl>
        </section>
        <section className="panel side-info side-tip request-next-step">
          <div className="request-summary-header">
            <img src={designAssets.icon.success} alt="" width={40} height={40} loading="lazy"/>
            <div><h2>Следующий шаг</h2>
              <p>{extracted ? 'Подбор по прайс-листу' : 'Распознавание товаров'}</p></div>
          </div>
          <p>{extracted
            ? 'Проверьте наименования и количества. Сервис предложит подходящие товары. Если данных недостаточно, позицию нужно будет уточнить.'
            : 'После ввода заявки распознайте товарные строки или добавьте их вручную.'}</p>
          {catalogError && <p className="request-catalog-warning" role="status">
            {isDemo ? 'Учебный прайс недоступен. Загрузите пример заново.' : 'Для расчёта нужен доступный прайс. Загрузите его или повторите попытку позже.'}
          </p>}
          {!catalog && !catalogError && <button type="button" className="button button-white"
            onClick={onImport}><UploadCloud size={17} aria-hidden="true"/> Загрузить прайс</button>}
        </section>
      </aside>
    </div>

    <div className="request-bottom-bar">
      <button className="button button-white" type="button" disabled={busy} onClick={onBack}>
        <ArrowLeft size={17} aria-hidden="true"/> Отмена
      </button>
      {!extracted ?
        <button className="button button-primary" type="submit" form="request-source-form"
          data-demo-target={isDemo ? 'extract' : undefined}
          disabled={busy || !requestText.trim()}>
          {extracting ? <LoaderCircle size={18} className="spin" aria-hidden="true"/> :
            <Sparkles size={18} aria-hidden="true"/>}
          {extracting ? 'Распознавание…' : 'Распознать заявку'} <ArrowRight size={17} aria-hidden="true"/>
        </button> :
        <button className="button button-primary" type="button" disabled={!canDraft} onClick={onDraft} data-demo-target={isDemo ? 'calculate' : undefined}>
          {drafting ? <LoaderCircle size={18} className="spin" aria-hidden="true"/> :
            <FileText size={18} aria-hidden="true"/>}
          {drafting ? 'Расчёт…' : 'Подобрать товары и рассчитать КП'}
          {!drafting && <ArrowRight size={17} aria-hidden="true"/>}
        </button>}
    </div>
  </div>;
}
