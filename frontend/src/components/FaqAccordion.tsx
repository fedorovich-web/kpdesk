import { useState } from 'react';

export interface FaqQuestion {
  q: string;
  a: string;
}

/** Share one controller between groups to keep the whole FAQ exclusive. */
export function useFaqAccordion() {
  const [openQuestion, setOpenQuestion] = useState<string | null>(null);
  // Let the browser handle scroll anchoring. Manually calling scrollBy after
  // changing <details> made pricing FAQ jump, especially near the page end.
  function toggle(question: string) {
    setOpenQuestion(current => current === question ? null : question);
  }

  return { openQuestion, toggle };
}

export function FaqItem({ item, accordion, className, hidden }: {
  item: FaqQuestion;
  accordion: ReturnType<typeof useFaqAccordion>;
  className?: string;
  hidden?: boolean;
}) {
  return <details className={className} hidden={hidden}
    open={accordion.openQuestion === item.q}>
    <summary onClick={event => {
      event.preventDefault();
      accordion.toggle(item.q);
    }}>{item.q}</summary>
    <p>{item.a}</p>
  </details>;
}

export function FaqAccordion({ items, className, itemClassName }: {
  items: readonly FaqQuestion[];
  className?: string;
  itemClassName?: string;
}) {
  const accordion = useFaqAccordion();
  return <div className={className}>
    {items.map(item => <FaqItem key={item.q} item={item} accordion={accordion} className={itemClassName}/>)}
  </div>;
}
