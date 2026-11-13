import { useState } from 'react';
import type { DragEvent, FormEvent } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, ChevronRight, FileSpreadsheet, LoaderCircle, UploadCloud } from 'lucide-react';
import { designAssets } from '../design-assets';

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const REQUIRED_COLUMNS = ['sku', 'name', 'category', 'sale_unit', 'pieces_per_sale_unit', 'price_rub'] as const;

export function catalogFileError(file: File, isDemo = false): string | null {
  if (isDemo && !/\.csv$/i.test(file.name)) return 'В демо разрешён только учебный CSV.';
  if (!/\.(csv|xlsx)$/i.test(file.name)) return 'Поддерживаются только CSV и XLSX.';
  if (file.size === 0) return 'Нельзя импортировать пустой файл.';
  if (isDemo && file.size > 1024 * 1024) return 'Учебный CSV должен быть до 1 МиБ.';
  if (file.size > MAX_UPLOAD_BYTES) return 'Файл больше 10 МиБ. Выберите файл меньшего размера.';
  return null;
}

export function ImportCatalogPage({ file, onFileChange, onSubmit, busy, onBack, isDemo = false }: {
  isDemo?: boolean;
  file: File | null;
  onFileChange: (file: File | null) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  busy: boolean;
  onBack: () => void;
}) {
  const [dragging, setDragging] = useState(false);
  const validation = file ? catalogFileError(file, isDemo) : null;
  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (busy) return;
    onFileChange(event.dataTransfer.files.item(0));
  }
  return <>
    <p className="breadcrumbs"><button type="button" onClick={onBack}>Прайс-лист</button>
      <ChevronRight size={15} aria-hidden="true"/> Импорт</p>
    <div className="heading-row">
      <div><h1>Импорт прайс-листа</h1>
        <p>{isDemo ? 'Откройте учебный прайс или загрузите точную копию примера CSV. Демо проверяет файл в браузере и не отправляет его на сервер.' : 'Загрузите Excel или CSV по шаблону. После проверки товары, цены и характеристики станут доступны для подбора.'}</p></div>
    </div>
    <ol className="stepper import-stepper" aria-label="Этапы импорта">
      {(isDemo ? ['Выбор учебного CSV', 'Проверка в браузере', 'Учебный прайс готов']
        : ['Выбор файла', 'Проверка товаров и цен', 'Прайс готов к подбору']).map((title, index) =>
        <li key={title} className={index === (busy ? 1 : 0) ? 'active' : busy && index === 0 ? 'done' : ''}>
          <span className="step-dot">{busy && index === 0 ? <CheckCircle2 size={16} aria-hidden="true"/> : index + 1}</span><span>{title}</span>
        </li>)}
    </ol>
    <form className="panel import-panel" onSubmit={onSubmit}>
      <div className="import-grid">
        <div className={'upload-box' + (dragging ? ' is-dragging' : '')}
          onDragEnter={event => { event.preventDefault(); if (!busy) setDragging(true); }}
          onDragOver={event => event.preventDefault()}
          onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
          onDrop={handleDrop}>
          <img src={designAssets.icon.upload} alt="" width={60} height={60} loading="lazy"/>
          <h2>Перетащите файл сюда</h2>
          <p>{isDemo ? 'или выберите скачанный учебный CSV на компьютере' : 'или выберите CSV / XLSX на компьютере'}</p>
          <label className="field import-file-label">Файл прайс-листа
            <input type="file" accept={isDemo ? '.csv' : '.csv,.xlsx'} disabled={busy}
              onChange={event => onFileChange(event.target.files?.[0] ?? null)}/>
          </label>
          {file && <p className="selected-file" role="status">
            {file.name} · {(file.size / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} КиБ
          </p>}
          {validation && <p className="upload-validation" role="alert">{validation}</p>}
          <p className="upload-hint">{isDemo ? 'Только точная копия учебного CSV, до 1 МиБ и 5 товаров. Другие файлы отклоняются; передачи на сервер нет.' : 'До 10 МиБ и 5000 товаров. Текущий прайс заменится новым, только если в файле не будет ошибок.'}</p>
        </div>
        <aside className="requirements">
          <div className="requirements-heading"><img src={designAssets.icon.documentInfo} alt="" width={40} height={40} loading="lazy"/>
            <h2>Требования к файлу</h2></div>
          {isDemo ? <>
            <ul>
              <li>Скачайте учебный CSV по ссылке ниже и загрузите его без изменений.</li>
              <li>Пять вымышленных товаров: три вида бумаги и две шариковые ручки.</li>
              <li>Демо принимает только файл, совпадающий с учебным примером.</li>
              <li>После активации откройте заявку; после обновления страницы демо сбрасывается.</li>
            </ul>
            <a href="/templates/kpdesk-portfolio-synthetic-v1.csv" download
              className="import-template-link"><FileSpreadsheet size={17} aria-hidden="true"/> Скачать учебный прайс CSV</a>
            <p>Чтобы загрузить свой прайс в Excel или CSV, <a href="/app">откройте личный кабинет</a>.</p>
          </> : <><ul>
            <li>CSV в UTF-8, разделитель «;», либо XLSX с единственным листом Products</li>
            <li>Сохраните названия и порядок столбцов из шаблона</li>
            <li>Обязательные столбцы: {REQUIRED_COLUMNS.join(', ')}</li>
            <li>Цена — число с точкой и максимум двумя знаками после неё</li>
            <li>В XLSX запрещены формулы и внешние связи</li>
            <li>Если хотя бы в одной строке есть ошибка, новая версия прайса не загружается</li>
          </ul>
          <a href="/templates/kpdesk-catalog-v2-template.xlsx" download="kpdesk-catalog-v2-template.xlsx"
            className="import-template-link"><FileSpreadsheet size={17} aria-hidden="true"/>
            Скачать шаблон Excel (.xlsx)</a>
          <a href="/templates/kpdesk-catalog-v2-columns.csv" download="kpdesk-catalog-v2-columns.csv"
            className="import-template-link"><FileSpreadsheet size={17} aria-hidden="true"/>
            Скачать шаблон CSV</a>
          <a href="/templates/kpdesk-catalog-v2-template.json" download="kpdesk-catalog-v2-template.json"
            className="import-template-link"><FileSpreadsheet size={17} aria-hidden="true"/>
            Скачать JSON-шаблон для подготовки данных</a>
          <p>Заполните товары на листе Products в шаблоне Excel или добавьте строки в CSV. JSON-шаблон можно использовать для подготовки и предварительной проверки данных; загрузить его как действующий прайс пока нельзя.</p>
          <p><strong>Другие прайсы и выгрузки 1С, Битрикс24, amoCRM</strong> проверьте в личном кабинете, в разделе работы с прайсами поставщиков. Здесь доступны файлы по шаблону. Неуказанные цены, единицы продажи и категории нужно уточнить перед загрузкой.</p>
          </>}
        </aside>
      </div>
      <div className="panel-footer import-footer">
        <button className="button button-white" type="button" onClick={onBack} disabled={busy}>
          <ArrowLeft size={17} aria-hidden="true"/> Назад
        </button>
        <button className="button button-primary" type="submit" disabled={!file || !!validation || busy}>
          {busy ? <LoaderCircle size={18} className="spin" aria-hidden="true"/> :
            <UploadCloud size={18} aria-hidden="true"/>}
          {busy ? isDemo ? 'Проверяем учебный прайс…' : 'Проверяем и загружаем…' : 'Проверить и загрузить'}
          {!busy && <ArrowRight size={17} aria-hidden="true"/>}
        </button>
      </div>
    </form>
  </>;
}
