import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '../tenant-session';
import type { TrainingFeedbackBundle, TrainingFeedbackHistory, TrainingFeedbackSummary } from '../tenant-session';

export function downloadFeedbackFile(bundle: TrainingFeedbackBundle) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(bundle)], { type:'application/json' }));
  const anchor=document.createElement('a');
  try {
    anchor.href=url; anchor.download='kpdesk-manager-feedback.json'; document.body.append(anchor); anchor.click();
  } finally { anchor.remove(); URL.revokeObjectURL(url); }
}

type Props = { organizationId: string; quoteId: string; csrf: string | null; scope: string; version: number };

export function QuoteTrainingFeedbackHistory({ organizationId, quoteId, csrf, scope, version }: Props) {
  const [history,setHistory]=useState<TrainingFeedbackHistory | null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const requestScope=JSON.stringify([scope,version]);
  const current=useRef({scope:requestScope,epoch:0});
  if(current.current.scope!==requestScope) current.current={scope:requestScope,epoch:current.current.epoch+1};
  const loaded=useRef('');
  const visibleHistory=loaded.current===requestScope ? history:null;
  const epoch=current.current.epoch;
  const mounted=useRef(true);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{loaded.current='';setHistory(null);setBusy(false);setError('');},[scope,version]);
  const fresh=(sent:number)=>mounted.current && current.current.epoch===sent;
  const load=async (before?:string)=>{
    if(!csrf || busy) return;
    const sent=epoch; setBusy(true);setError('');
    try {
      const result=await sessionApi.trainingFeedbackHistory(organizationId,quoteId,csrf,before);
      if(!fresh(sent)) return;
      if(result.items.some(item=>item.quote_id!==quoteId)) throw Error('Получена история другого КП.');
      loaded.current=requestScope;
      setHistory(old=>({items:before ? [...(old?.items ?? []),...result.items] : result.items,next_cursor:result.next_cursor}));
    } catch(cause){if(fresh(sent)) setError(cause instanceof Error ? cause.message:'История недоступна.');}
    finally{if(fresh(sent)) setBusy(false);}
  };
  const act=async (item:TrainingFeedbackSummary,withdraw:boolean)=>{
    if(!csrf || busy) return;
    const sent=epoch;setBusy(true);setError('');
    try {
      if(withdraw){
        const result=await sessionApi.withdrawTrainingFeedback(organizationId,quoteId,item.feedback_id,csrf);
        if(!fresh(sent)) return;
        if(result.feedback_id!==item.feedback_id || result.quote_id!==quoteId || !result.withdrawn_at)
          throw Error('Получены другие оценки. Обновите историю.');
        setHistory(old=>old && {...old,items:old.items.map(row=>row.feedback_id===result.feedback_id ? result:row)});
      } else {
        const result=await sessionApi.downloadSavedTrainingFeedback(organizationId,quoteId,item.feedback_id,csrf);
        if(!fresh(sent)) return;
        if(result.organization_id!==organizationId || result.quote_id!==quoteId || result.revision!==item.revision
          || result.format!=='kpdesk-manager-feedback-v1' || !result.training_consent)
          throw Error('Полученные оценки относятся к другой записи. Обновите историю.');
        downloadFeedbackFile(result);
      }
    } catch(cause){if(fresh(sent)) setError(cause instanceof Error ? cause.message:'Действие недоступно.');}
    finally{if(fresh(sent)) setBusy(false);}
  };
  return <details className="training-feedback-history">
    <summary>История сохранённых оценок</summary>
    <p>Оценки привязаны к версии КП. После отзыва согласия эти оценки нельзя будет скачать повторно.
      История сохраняется. Ранее скачанные файлы останутся у получателя.</p>
    {error && <p role="alert" className="alert error">{error}</p>}
    <button type="button" className="button button-white" disabled={busy || !csrf}
      onClick={()=>void load()}>Загрузить историю оценок</button>
    {visibleHistory && !visibleHistory.items.length && <p>Сохранённых оценок пока нет.</p>}
    {visibleHistory?.items.map(item=><div key={item.feedback_id} className="training-feedback-history-item">
      <p>Версия {item.revision} · {new Date(item.created_at).toLocaleString('ru-RU')} · оценок: {item.event_count}.
        {' '}{item.withdrawn_at ? 'Согласие отозвано.' : 'Сохранено для подготовки обучающих данных. Подбор не изменён.'}</p>
      {!item.withdrawn_at && <>
        <button type="button" className="button button-white" disabled={busy || !csrf}
          onClick={()=>void act(item,false)}>Скачать сохранённые оценки</button>{' '}
        <button type="button" className="text-button" disabled={busy || !csrf}
          onClick={()=>void act(item,true)}>Отозвать согласие на обучение</button>
      </>}
    </div>)}
    {visibleHistory?.next_cursor && <button type="button" className="button button-white" disabled={busy || !csrf}
      onClick={()=>void load(visibleHistory.next_cursor ?? undefined)}>Показать ещё оценки</button>}
  </details>;
}
