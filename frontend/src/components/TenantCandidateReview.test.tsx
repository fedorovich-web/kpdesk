import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionApi } from '../tenant-session';
import type { CandidateInspectResponse } from '../tenant-session';
import { TenantCandidateReview } from './TenantCandidateReview';

const file = new File(['zip bytes'], 'original-evidence.zip', { type: 'application/zip' });
const inspect: CandidateInspectResponse = {
  archive_sha256: 'a'.repeat(64),
  queue_sha256: 'b'.repeat(64),
  total_requests: 2, offset: 0, next_offset: null,
  rights_verified: false, privacy_review_complete: false,
  independent_gold: false, training_approved: false,
  tasks: [{
    request_id: 'REQ-1', task_sha256: 'c'.repeat(64),
    request_text: 'Нужны скрепки, пачки по 100 штук',
    partition: 'train', source_sha256: 'd'.repeat(64),
    source_batch_id: 'batch-1',
    candidate_skus: ['SKU-A', 'SKU-B'],
    candidates: [
      { sku: 'SKU-A', name: 'Скрепки 100 шт', sale_unit: 'пачка', pieces_per_sale_unit: 100 },
      { sku: 'SKU-B', name: 'Бумага А4', sale_unit: 'штука', pieces_per_sale_unit: 1 },
    ],
  }],
};

afterEach(() => vi.restoreAllMocks());

describe('tenant source-locked candidate annotations', () => {
  it('does not produce gold and requires explicit judgment for every shown candidate', async () => {
    const inspectSpy = vi.spyOn(sessionApi, 'inspectCandidateReview')
      .mockResolvedValue(inspect);
    const attest = vi.spyOn(sessionApi, 'attestCandidateReview').mockResolvedValue({
      recorded: true, queue_sha256: inspect.queue_sha256,
      task_sha256: inspect.tasks[0]!.task_sha256, request_id: 'REQ-1',
      reviewer_user_id: 'user-1', rights_verified: false,
      independently_verified: false, privacy_review_complete: false,
      independent_gold: false, training_approved: false,
      benchmark_approved: false,
    });
    render(<TenantCandidateReview organizationId="org-1" csrf="csrf-1"/>);
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить и открыть очередь' }));
    await waitFor(() => expect(inspectSpy).toHaveBeenCalledWith(
      'org-1', file, 'csrf-1', 0,
    ));
    expect(await screen.findByText(/Права и приватность источников/)).toBeInTheDocument();
    expect(attest).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Оценить товары' }));
    const submit = screen.getByRole('button', { name: 'Записать личную оценку' });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/SKU-A: Скрепки/), {
      target: { value: 'rejected' },
    });
    fireEvent.change(screen.getByLabelText(/SKU-B: Бумага/), {
      target: { value: 'rejected' },
    });
    fireEvent.change(screen.getByLabelText('Обоснование оценки по оригинальной заявке'), {
      target: { value: 'Все предложенные позиции проверены по условиям исходной заявки.' },
    });
    fireEvent.click(screen.getByLabelText(/Я лично проверил оригинал заявки/));
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/ни один из показанных товаров не подходит/));
    fireEvent.click(submit);
    await waitFor(() => expect(attest).toHaveBeenCalledWith(
      'org-1', file, 'csrf-1', inspect, inspect.tasks[0],
      { 'SKU-A': 'rejected', 'SKU-B': 'rejected' },
      'Все предложенные позиции проверены по условиям исходной заявки.', true,
    ));
    expect(await screen.findByText(/Это не разрешение на обучение модели или публикацию данных/))
      .toBeInTheDocument();
  });

  it('invalidates prior inspection when original archive changes', async () => {
    vi.spyOn(sessionApi, 'inspectCandidateReview').mockResolvedValue(inspect);
    render(<TenantCandidateReview organizationId="org-1" csrf="csrf-1"/>);
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить и открыть очередь' }));
    expect(await screen.findByText(/Права и приватность источников/))
      .toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [new File(['changed'], 'different.zip')] },
    });
    expect(screen.queryByText(/Права и приватность источников/))
      .not.toBeInTheDocument();
  });

  it('shows authenticated consensus and only non-authorizing source restrictions', async () => {
    vi.spyOn(sessionApi, 'inspectCandidateReview').mockResolvedValue(inspect);
    const consensus = vi.spyOn(sessionApi, 'candidateConsensus').mockResolvedValue({
      queue_sha256: inspect.queue_sha256, source_bound_review_count: 2,
      agreed: [{
        request_id: 'REQ-1', task_sha256: inspect.tasks[0]!.task_sha256,
        reviewer_count: 2, reviewer_user_ids: ['user-a', 'user-b'],
        accepted_skus: [], rejected_skus: ['SKU-A', 'SKU-B'],
        no_candidate_match_among_proposals: true,
      }],
      conflicts: [], incomplete: [],
      rights_verified: false, privacy_review_complete: false,
      independent_gold: false, training_approved: false, benchmark_approved: false,
    });
    const restrictions = vi.spyOn(sessionApi, 'candidateSourceRestrictions')
      .mockResolvedValue({
        rows: [], next_offset: null, rights_verified: false,
        privacy_review_complete: false, training_approved: false,
      });
    render(<TenantCandidateReview organizationId="org-1" csrf="csrf-1"/>);
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить и открыть очередь' }));
    expect(await screen.findByText(/Права и приватность источников ещё требуют/))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {
      name: 'Согласование оценок и ограничения использования',
    }));
    await waitFor(() => expect(consensus).toHaveBeenCalledWith(
      'org-1', inspect.queue_sha256,
    ));
    expect(restrictions).toHaveBeenCalledWith('org-1', inspect.queue_sha256);
    expect(await screen.findByText(/Совпали оценки двух проверяющих сотрудников/))
      .toBeInTheDocument();
    expect(screen.getByText(/Обучение модели запрещено/)).toBeInTheDocument();
  });

  it('never records a rights grant and requires explicit source restriction confirmation', async () => {
    vi.spyOn(sessionApi, 'inspectCandidateReview').mockResolvedValue(inspect);
    const restrict = vi.spyOn(sessionApi, 'attestCandidateSourceRestriction')
      .mockResolvedValue({
        recorded: true, source_sha256: inspect.tasks[0]!.source_sha256,
        review_kind: 'rights', finding: 'restricted', reviewer_user_id: 'user-1',
        rights_verified: false, privacy_review_complete: false,
        training_approved: false,
      });
    render(<TenantCandidateReview organizationId="org-1" csrf="csrf-1"/>);
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Проверить и открыть очередь' }));
    expect(await screen.findByText(/Права и приватность источников ещё требуют/))
      .toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Записать ограничение исходника' });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText('SHA-256 исходного документа'), {
      target: { value: inspect.tasks[0]!.source_sha256 },
    });
    fireEvent.change(screen.getByLabelText('Ограничение'), {
      target: { value: 'restricted' },
    });
    fireEvent.change(screen.getByLabelText('Обоснование ограничения по оригиналу'), {
      target: {
        value: 'Оригинал содержит условия, ограничивающие коммерческое повторное использование.',
      },
    });
    fireEvent.click(screen.getByLabelText(/Я лично проверил исходный документ/));
    fireEvent.click(save);
    await waitFor(() => expect(restrict).toHaveBeenCalledWith(
      'org-1', file, 'csrf-1', inspect, inspect.tasks[0]!.source_sha256,
      'rights', 'restricted', null,
      'Оригинал содержит условия, ограничивающие коммерческое повторное использование.',
    ));
    expect(await screen.findByText(/Это не подтверждение прав или разрешение на обучение/))
      .toBeInTheDocument();
  });

  it('cannot inspect when CSRF is missing', () => {
    const inspectSpy = vi.spyOn(sessionApi, 'inspectCandidateReview');
    render(<TenantCandidateReview organizationId="org-1" csrf={null}/>);
    fireEvent.change(screen.getByLabelText(/Оригиналы заявок и очередь/), {
      target: { files: [file] },
    });
    expect(screen.getByRole('button', { name: 'Проверить и открыть очередь' }))
      .toBeDisabled();
    expect(inspectSpy).not.toHaveBeenCalled();
  });
});
