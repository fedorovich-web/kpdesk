import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { CandidateConsensus, CandidateInspectResponse } from '../tenant-session';
import { TenantCandidateAdjudication } from './TenantCandidateAdjudication';

const archive = new File(['test-evidence'], 'test.zip');
const task = {
  request_id: 'REQUEST-1', task_sha256: 'b'.repeat(64),
  source_sha256: 'c'.repeat(64), source_batch_id: 'batch-1',
  request_text: 'Нужна бумага для офисного принтера А4',
  partition: 'train' as const, candidate_skus: ['SKU-A', 'SKU-B'],
  candidates: [
    { sku: 'SKU-A', name: 'Бумага А4', sale_unit: 'пачка' as const, pieces_per_sale_unit: 500 },
    { sku: 'SKU-B', name: 'Картон А3', sale_unit: 'штука' as const, pieces_per_sale_unit: 1 },
  ],
};
const inspection: CandidateInspectResponse = {
  archive_sha256: 'a'.repeat(64), queue_sha256: 'd'.repeat(64),
  total_requests: 1, offset: 0, next_offset: null, tasks: [task],
  rights_verified: false, privacy_review_complete: false,
  independent_gold: false, training_approved: false,
};
const consensus: CandidateConsensus = {
  queue_sha256: inspection.queue_sha256, source_bound_review_count: 2,
  agreed: [], incomplete: [], conflicts: [{
    request_id: task.request_id, task_sha256: task.task_sha256,
    reviewer_count: 2, reviewer_user_ids: ['reviewer-a', 'reviewer-b'],
  }],
  rights_verified: false, privacy_review_complete: false,
  independent_gold: false, training_approved: false, benchmark_approved: false,
};
const props = {
  organizationId: 'org-1', csrf: 'csrf-1', archive, inspection, consensus,
};
afterEach(() => vi.restoreAllMocks());

describe('independent owner dispute adjudication', () => {
  it('requires explicit complete judgment, rationale, and personal confirmation', async () => {
    const save = vi.spyOn(sessionApi, 'attestCandidateAdjudication').mockResolvedValue({
      recorded: true, queue_sha256: inspection.queue_sha256,
      task_sha256: task.task_sha256, request_id: task.request_id,
      finding: 'resolve_proposals', adjudicator_user_id: 'owner-3',
      reviewer_independence_verified: true,
      rights_verified: false, privacy_review_complete: false,
      independent_gold: false, training_approved: false, benchmark_approved: false,
    });
    render(<TenantCandidateAdjudication {...props}/>);
    expect(screen.queryByRole('button', { name: 'Записать независимое решение' }))
      .not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Заявка со спорной оценкой'), {
      target: { value: task.task_sha256 },
    });
    const submit = screen.getByRole('button', { name: 'Записать независимое решение' });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Решение по спору'), {
      target: { value: 'resolve_proposals' },
    });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/SKU-A — Бумага/), {
      target: { value: 'accepted' },
    });
    fireEvent.change(screen.getByLabelText(/SKU-B — Картон/), {
      target: { value: 'rejected' },
    });
    fireEvent.change(screen.getByLabelText('Обоснование независимого решения'), {
      target: {
        value: 'Лично сравнил два противоречивых решения с исходной заявкой и характеристиками товаров.',
      },
    });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я лично изучил оригинал и оба/));
    fireEvent.click(submit);
    await waitFor(() => expect(save).toHaveBeenCalledWith(
      'org-1', archive, 'csrf-1', inspection, task, 'resolve_proposals',
      { 'SKU-A': 'accepted', 'SKU-B': 'rejected' }, false,
      'Лично сравнил два противоречивых решения с исходной заявкой и характеристиками товаров.',
    ));
    expect(await screen.findByText(/Обучение запрещено/)).toBeInTheDocument();
  });

  it('does not adjudicate incomplete one-reviewer tasks', () => {
    const save = vi.spyOn(sessionApi, 'attestCandidateAdjudication');
    render(<TenantCandidateAdjudication {...props} consensus={{
      ...consensus,
      conflicts: [],
      incomplete: [{
        request_id: task.request_id, task_sha256: task.task_sha256,
        reviewer_count: 1, reviewer_user_ids: ['one'],
      }],
    }}/>);
    expect(screen.queryByRole('option', { name: /REQUEST-1/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Записать независимое решение' }))
      .not.toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });

  it('reads immutable history without treating it as training approval', async () => {
    const history = vi.spyOn(sessionApi, 'candidateAdjudicationHistory').mockResolvedValue({
      rows: [], next_offset: null, independent_gold: false, training_approved: false,
    });
    render(<TenantCandidateAdjudication {...props}/>);
    fireEvent.click(screen.getByRole('button', { name: 'История независимых решений' }));
    await waitFor(() => expect(history).toHaveBeenCalledWith(
      'org-1', inspection.queue_sha256, 0,
    ));
    expect(screen.getByText(/Проверенный набор для обучения не создан/)).toBeInTheDocument();
  });
});
