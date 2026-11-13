import { useMemo, useState } from 'react';
import { AlertCircle, ArrowLeft, ArrowRight, FileSpreadsheet, Layers3, PackageSearch, RotateCcw, Search, UploadCloud } from 'lucide-react';
import { categoryLabel } from '../categories';
import { money } from '../domain';
import type { Catalog, CatalogProduct } from '../domain';
import { designAssets } from '../design-assets';

type SortKey = 'name' | 'sku' | 'price';
const PAGE_SIZES = [8, 20, 50] as const;

export function CatalogPage({ catalog, products, query, onQueryChange, onImport, loadError, isDemo = false }: {
  isDemo?: boolean;
  catalog: Catalog | null;
  products: CatalogProduct[];
  query: string;
  onQueryChange: (query: string) => void;
  onImport: () => void;
  loadError?: string | null;
}) {
  const [category, setCategory] = useState('');
  const [sort, setSort] = useState<SortKey>('name');
  const [descending, setDescending] = useState(false);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZES[0]);
  const [page, setPage] = useState(1);
  const categories = useMemo(() => [...new Set(products.map(item => item.category))].sort(), [products]);
  const rows = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU');
    return products.filter(item =>
      (!category || item.category === category)
      && (!needle || [item.sku, item.name, item.brand ?? '']
        .some(text => text.toLocaleLowerCase('ru-RU').includes(needle)))
    ).sort((a, b) => {
      const comparison = sort === 'price'
        ? Number(a.price_rub) - Number(b.price_rub)
        : (sort === 'sku' ? a.sku.localeCompare(b.sku, 'ru') : a.name.localeCompare(b.name, 'ru'));
      return descending ? -comparison : comparison;
    });
  }, [products, query, category, sort, descending]);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pages);
  const start = (currentPage - 1) * pageSize;
  const visibleRows = rows.slice(start, start + pageSize);
  function changeSort(key: SortKey) {
    if (sort === key) setDescending(prev => !prev);
    else { setSort(key); setDescending(false); }
    setPage(1);
  }
  function clearFilters() {
    onQueryChange('');
    setCategory('');
    setPage(1);
  }

  return <>
    <div className="heading-row">
      <div><h1>Прайс-лист</h1><p>{isDemo ? 'Учебный прайс из пяти вымышленных товаров. Найдите товар по названию, артикулу или бренду.' : 'Ваши товары, цены и характеристики из загруженного прайса. Найдите товар по названию, артикулу или бренду.'}</p></div>
      <div className="heading-actions">
        <button className="button button-primary" type="button" onClick={onImport}>
          <UploadCloud size={18} aria-hidden="true"/> Загрузить прайс
        </button>
      </div>
    </div>
    {loadError ? <section className="panel catalog-unavailable" aria-label="Состояние прайс-листа">
      <AlertCircle size={32} aria-hidden="true"/>
      <h2>Не удалось открыть прайс</h2>
      <p>Товары и сведения о прайсе временно недоступны.</p>
      <p>Проверьте соединение и попробуйте ещё раз.</p>
    </section> : <><div className="metric-grid prices-metrics catalog-stat-grid">
      <div className="metric-card"><span className="metric-icon"><img src={designAssets.icon.database} alt="" width={48} height={48}/></span>
        <div><span>Всего товаров</span><strong>{products.length}</strong><small>В загруженном прайсе</small></div></div>
      <div className="metric-card"><span className="metric-icon"><img src={designAssets.icon.success} alt="" width={48} height={48}/></span>
        <div><span>Найдено по фильтрам</span><strong>{rows.length}</strong><small>По выбранным условиям</small></div></div>
      <div className="metric-card"><span className="metric-icon"><img src={designAssets.icon.quotes} alt="" width={48} height={48}/></span>
        <div><span>Категорий</span><strong>{categories.length}</strong><small>{isDemo ? 'По учебному прайсу' : 'В текущем прайсе'}</small></div></div>
      <div className="metric-card"><span className="metric-icon"><Layers3 size={24} aria-hidden="true"/></span>
        <div><span>Текущая версия</span><strong className="metric-text">{catalog ? 'Есть' : 'Нет'}</strong>
          <small>{isDemo ? 'Только демонстрация' : catalog ? 'Доступна для подбора' : 'Прайс не загружен'}</small></div></div>
    </div>
    <section className="panel catalog-panel" aria-label="Товары каталога" data-demo-target={isDemo ? 'catalog' : undefined}>
      <div className="toolbar catalog-toolbar">
        <label className="field toolbar-search" data-demo-target={isDemo ? 'catalog-search' : undefined}>Поиск товаров
          <span className="catalog-search-wrap"><Search size={18} aria-hidden="true"/>
            <input type="search" value={query} onChange={event => {
              onQueryChange(event.target.value); setPage(1);
            }} placeholder="Наименование, артикул, бренд"/></span>
        </label>
        <label className="field">Категория
          <select value={category} onChange={event => { setCategory(event.target.value); setPage(1); }}>
            <option value="">Все категории</option>
            {categories.map(value => <option value={value} key={value}>{categoryLabel(value)}</option>)}
          </select>
        </label>
        <button className="button button-white toolbar-reset" type="button"
          onClick={clearFilters} disabled={!query && !category}>
          <RotateCcw size={17} aria-hidden="true"/> Сбросить фильтры
        </button>
      </div>
      {!products.length ? <div className="empty-state catalog-empty">
        <FileSpreadsheet size={36} aria-hidden="true"/><h2>Прайс-лист пуст</h2>
        <p>{isDemo ? 'Активируйте учебный прайс или загрузите точную копию учебного CSV.' : 'Загрузите прайс CSV или Excel с названиями товаров, артикулами, ценами и характеристиками.'}</p>
        <button className="button button-primary" type="button" onClick={onImport}>Загрузить прайс <ArrowRight size={17} aria-hidden="true"/></button>
      </div> : !rows.length ? <div className="empty-state catalog-empty">
        <PackageSearch size={36} aria-hidden="true"/><h2>Ничего не найдено</h2>
        <p>Измените запрос или сбросьте фильтры, чтобы увидеть другие товары.</p>
        <button className="button button-white" type="button" onClick={clearFilters}>Сбросить фильтры</button>
      </div> : <>
        <div className="table-scroll" role="region" tabIndex={0} aria-label="Прокручиваемая таблица товаров">
          <table className="data-table catalog-table">
            <thead><tr>
              <th scope="col"><button type="button" aria-label="Сортировать по названию" onClick={() => changeSort('name')}>Товар {sort === 'name' && (descending ? '↓' : '↑')}</button></th>
              <th scope="col"><button type="button" aria-label="Сортировать по артикулу" onClick={() => changeSort('sku')}>Артикул {sort === 'sku' && (descending ? '↓' : '↑')}</button></th>
              <th scope="col">Категория</th>
              <th scope="col"><button type="button" aria-label="Сортировать по цене" onClick={() => changeSort('price')}>Цена, ₽ {sort === 'price' && (descending ? '↓' : '↑')}</button></th>
              <th scope="col">Единица продажи</th>
            </tr></thead>
            <tbody>{visibleRows.map(item => <tr key={item.sku}>
              <td><strong>{item.name}</strong>{item.brand && <small>{item.brand}</small>}</td>
              <td className="muted">{item.sku}</td>
              <td>{categoryLabel(item.category)}</td>
              <td className="price-cell">{money(item.price_rub)}</td>
              <td>{item.sale_unit}{item.pieces_per_sale_unit > 1 && ' · ' + item.pieces_per_sale_unit + ' шт.'}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <div className="catalog-pagination">
          <p role="status">Показано {start + 1}–{Math.min(start + pageSize, rows.length)} из {rows.length}</p>
          <div className="catalog-pager">
            <button className="button button-white" type="button" aria-label="Предыдущая страница"
              disabled={currentPage === 1} onClick={() => setPage(p => Math.max(1, p - 1))}><ArrowLeft size={17}/></button>
            <span>Страница {currentPage} из {pages}</span>
            <button className="button button-white" type="button" aria-label="Следующая страница"
              disabled={currentPage === pages} onClick={() => setPage(p => Math.min(pages, p + 1))}><ArrowRight size={17}/></button>
          </div>
          <label className="field catalog-page-size">Показывать
            <select value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1); }}>
              {PAGE_SIZES.map(size => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
        </div>
      </>}
    </section></>}
  </>;
}
