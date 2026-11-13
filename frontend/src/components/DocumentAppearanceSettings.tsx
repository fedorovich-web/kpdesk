import { useId } from 'react';
import type { DocumentAppearance } from '../tenant-session';

export const defaultDocumentAppearance: DocumentAppearance = {
  template: 'presentation', accent_color: '#008B50', show_company_section: true, layout_version: 2,
  default_logo: 'kpdesk_v1',
};

type Props = {
  value: DocumentAppearance; disabled: boolean; hasCompanyFacts: boolean;
  onChange: (value: DocumentAppearance) => void;
};

export function DocumentAppearanceSettings({ value, disabled, hasCompanyFacts, onChange }: Props) {
  const group = useId();
  return <fieldset className="document-appearance" disabled={disabled}>
    <legend>Оформление КП</legend>
    <p>Выберите шаблон и фирменный цвет. Оформление сохранится в этой версии КП.</p>
    {value.layout_version !== 2 && <p>Используется прежнее оформление сохранённого шаблона.
      <button type="button" className="text-button" onClick={() => onChange({ ...value, layout_version: 2, default_logo: 'kpdesk_v1' })}>
        Применить новое оформление КП</button></p>}
    <div className="document-layout-options">
      <label className="document-layout-option">
        <input type="radio" name={group} checked={value.template === 'presentation'}
          onChange={() => onChange({ ...value, template: 'presentation' })}/>
        <span><strong>Подробное предложение</strong><small>Заголовок, описание товаров и состав заказа.</small></span>
      </label>
      <label className="document-layout-option">
        <input type="radio" name={group} checked={value.template === 'compact'}
          onChange={() => onChange({ ...value, template: 'compact' })}/>
        <span><strong>Краткое предложение</strong><small>Товары и стоимость в компактном документе.</small></span>
      </label>
    </div>
    <label className="document-accent-control">Фирменный цвет
      <input type="color" value={value.accent_color}
        onChange={event => onChange({ ...value, accent_color: event.target.value.toUpperCase() })}/>
      <output>{value.accent_color}</output>
    </label>
    <label className="tenant-checkbox"><input type="checkbox" checked={value.show_company_section}
      onChange={event => onChange({ ...value, show_company_section: event.target.checked })}/>
      Добавить блок «О компании» с описанием и преимуществами из профиля
    </label>
    {!hasCompanyFacts && <p>Описание и преимущества можно заполнить в настройках компании.
      В КП попадут только сохранённые сведения.</p>}
  </fieldset>;
}
