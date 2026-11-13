export interface AttrSpec { key: string; label: string; type: 'text' | 'number' | 'list'; placeholder?: string }
export interface CategorySpec { id: string; label: string; fields: AttrSpec[] }
const t = (key: string, label: string): AttrSpec => ({ key, label, type: 'text' });
const n = (key: string, label: string, placeholder?: string): AttrSpec => ({ key, label, type: 'number',
  ...(placeholder ? { placeholder } : {}),
});
const l = (key: string, label: string): AttrSpec => ({ key, label, type: 'list' });
export const categories: CategorySpec[] = [
  { id:'paper',label:'Офисная бумага',fields:[] },
  { id:'pen',label:'Ручки',fields:[t('pen_type','Тип ручки'),n('tip_mm_x10','Толщина пишущего узла, десятые мм','Например, 7 для 0,7 мм')] },
  { id:'pencil',label:'Карандаши',fields:[t('pencil_type','Тип карандаша'),t('lead_grade','Твёрдость')] },
  { id:'mechanical_pencil',label:'Механические карандаши',fields:[t('lead_grade','Твёрдость'),n('lead_diameter_mm_x10','Диаметр грифеля, десятые мм','Например, 5 для 0,5 мм')] },
  { id:'marker',label:'Маркеры',fields:[t('ink_color','Цвет'),t('marker_type','Тип'),n('tip_mm_x10','Толщина пишущего узла, десятые мм','Например, 7 для 0,7 мм')] },
  { id:'highlighter',label:'Текстовыделители',fields:[t('ink_color','Цвет'),t('tip_shape','Форма наконечника')] },
  { id:'eraser',label:'Ластики',fields:[t('eraser_type','Тип')] },
  { id:'sharpener',label:'Точилки',fields:[t('sharpener_type','Тип'),n('holes','Отверстий')] },
  { id:'notebook',label:'Тетради',fields:[t('format','Формат'),n('sheets','Листов'),t('ruling','Разлиновка')] },
  { id:'notepad',label:'Блокноты',fields:[t('format','Формат'),n('sheets','Листов'),t('ruling','Разлиновка')] },
  { id:'document_folder',label:'Папки для документов',fields:[t('folder_kind','Тип папки'),t('format','Формат'),t('material','Материал')] },
  { id:'binder',label:'Папки-кольцевые',fields:[t('format','Формат'),n('rings','Колец'),n('spine_mm','Корешок, мм')] },
  { id:'plastic_sleeve',label:'Файлы-вкладыши',fields:[t('format','Формат'),n('thickness_microns','Толщина, мкм')] },
  { id:'envelope',label:'Конверты',fields:[t('format','Формат'),t('color','Цвет')] },
  { id:'label',label:'Этикетки',fields:[t('format','Формат'),n('labels_per_sheet','На листе'),t('printer_technology','Тип принтера')] },
  { id:'adhesive_tape',label:'Скотч и ленты',fields:[n('width_mm','Ширина, мм'),n('length_m','Длина, м'),t('tape_type','Тип ленты')] },
  { id:'glue',label:'Клей',fields:[t('glue_type','Тип'),n('volume_ml','Объём, мл')] },
  { id:'correction_fluid',label:'Корректоры',fields:[t('correction_type','Тип')] },
  { id:'staple',label:'Скобы',fields:[t('staple_size','Размер'),n('pack_count','В упаковке')] },
  { id:'scissors',label:'Ножницы',fields:[n('length_mm','Длина, мм')] },
  { id:'ruler',label:'Линейки',fields:[n('length_mm','Длина, мм')] },
  { id:'folder',label:'Папки-регистраторы',fields:[n('spine_mm','Корешок, мм'),t('format','Формат')] },
  { id:'stapler',label:'Степлеры',fields:[n('capacity_sheets','Листов')] },
  { id:'paperclip',label:'Скрепки',fields:[n('size_mm','Размер, мм'),n('pack_count','В пачке')] },
  { id:'keyboard',label:'Клавиатуры',fields:[t('connection','Подключение'),n('keys','Клавиш')] },
  { id:'mouse',label:'Мыши',fields:[t('connection','Подключение'),n('dpi','Разрешение DPI')] },
  { id:'mouse_pad',label:'Коврики для мыши',fields:[t('surface_material','Материал поверхности'),n('width_mm','Ширина, мм'),n('height_mm','Высота, мм')] },
  { id:'pen_refill',label:'Стержни для ручек',fields:[t('ink_color','Цвет чернил'),t('refill_type','Тип стержня')] },
  { id:'usb_adapter',label:'USB-переходники',fields:[t('connector_from','Разъём вход'),t('connector_to','Разъём выход')] },
  { id:'notebook_cover',label:'Обложки для тетрадей',fields:[t('format','Формат'),t('material','Материал')] },
  { id:'staple_remover',label:'Скобоудалители',fields:[t('remover_type','Тип')] },
  { id:'marker_refill',label:'Заправки для маркеров',fields:[t('refill_type','Тип заправки'),t('ink_color','Цвет')] },
  { id:'usb_drive',label:'USB-накопители',fields:[n('capacity_gb','Объём, ГБ'),t('usb_standard','Стандарт USB')] },
  { id:'toner_cartridge',label:'Лазерные картриджи',fields:[t('cartridge_code','Артикул'),t('printer_model','Модель принтера'),l('compatible_models','Совместимые модели'),t('color','Цвет'),t('oem_status','Оригинал / совместимый')] },
  { id:'ink_cartridge',label:'Струйные картриджи',fields:[t('cartridge_code','Артикул'),t('printer_model','Модель принтера'),l('compatible_models','Совместимые модели'),t('color','Цвет'),t('oem_status','Оригинал / совместимый')] },
  { id:'printer',label:'Принтеры',fields:[t('print_technology','Технология'),t('color_mode','Цветность'),t('max_format','Макс. формат')] },
  { id:'mfp',label:'МФУ',fields:[t('print_technology','Технология'),t('color_mode','Цветность'),t('max_format','Макс. формат')] },
  { id:'copier',label:'Копиры',fields:[t('print_technology','Технология'),t('color_mode','Цветность'),t('max_format','Макс. формат')] },
  { id:'photo_paper',label:'Фотобумага',fields:[t('format','Формат'),n('gsm','Плотность, г/м²'),t('finish','Покрытие')] },
  { id:'stationery_other',label:'Прочая канцелярия',fields:[t('product_kind','Вид товара')] },
];
export function categoryLabel(id: string | null): string {
  if (!id) return 'Не определена';
  return categories.find((item) => item.id === id)?.label ?? id;
}
