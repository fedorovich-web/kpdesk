import { useLayoutEffect, useRef, useState } from 'react';

export interface FaqQuestion {
  q: string;
  a: string;
}

/** Share one controller between groups to keep the whole FAQ exclusive. */
export function useFaqAccordion() {
  const [openQuestion, setOpenQuestion] = useState<string | null>(null);
  const anchor = useRef<{ summary: HTMLElement; top: number } | null>(null);

  useLayoutEffect(() => {
    const previous = anchor.current;
    anchor.current = null;
    if (!previous?.summary.isConnected) return;

    const delta = previous.summary.getBoundingClientRect().top - previous.top;
    // Correct before paint. 'instant' also overrides the public site's smooth scroll.
    // Measuring after layout accounts for any scroll anchoring already applied.
    if (Math.abs(delta) > 0.5) window.scrollBy({ top: delta, behavior: 'instant' });
  }, [openQuestion]);

  function toggle(question: string, summary: HTMLElement) {
    anchor.current = { summary, top: summary.getBoundingClientRect().top };
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
    open={accordion.openQuestion === item.q} style={{ overflowAnchor: 'none' }}>
    <summary onClick={event => {
      event.preventDefault();
      accordion.toggle(item.q, event.currentTarget);
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
