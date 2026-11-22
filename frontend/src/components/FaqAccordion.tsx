export interface FaqQuestion {
  q: string;
  a: string;
}

export function FaqItem({ item, name, className, hidden }: {
  item: FaqQuestion;
  name: string;
  className?: string;
  hidden?: boolean;
}) {
  return <details name={name} className={className} hidden={hidden}>
    <summary>{item.q}</summary>
    <p>{item.a}</p>
  </details>;
}

export function FaqAccordion({ items, name, className, itemClassName }: {
  items: readonly FaqQuestion[];
  name: string;
  className?: string;
  itemClassName?: string;
}) {
  return <div className={className}>
    {items.map(item => <FaqItem key={item.q} item={item} name={name} className={itemClassName}/>)}
  </div>;
}
