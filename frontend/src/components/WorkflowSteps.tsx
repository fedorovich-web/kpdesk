import { Check } from 'lucide-react';

const steps = ['Заявка клиента', 'Проверка товаров', 'Черновик КП'] as const;

export function WorkflowSteps({ active }: { active: 1 | 2 | 3 }) {
  return <ol className="stepper" aria-label="Этапы подготовки коммерческого предложения">
    {steps.map((name, index) => {
      const number = index + 1;
      return <li key={name} className={number === active ? 'active' : number < active ? 'done' : ''}
        aria-current={number === active ? 'step' : undefined}>
        <span className="step-dot">
          {number < active ? <Check size={16} strokeWidth={3} aria-hidden="true"/> : number}
        </span>
        <span>{name}</span>
      </li>;
    })}
  </ol>;
}
