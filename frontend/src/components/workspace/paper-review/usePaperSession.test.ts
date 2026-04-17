import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { SessionRun, PipelineTask } from '@/types';

vi.mock('@/api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));

import { apiGet, apiPost } from '@/api/client';
import { usePaperSession } from './usePaperSession';

const apiGetMock = vi.mocked(apiGet);
const apiPostMock = vi.mocked(apiPost);

function makeRun(overrides: Partial<SessionRun> = {}): SessionRun {
  return {
    run_id: 'run-1',
    status: 'completed',
    pipeline: [],
    ...overrides,
  };
}

function writerTask(paper_version?: number): PipelineTask {
  return {
    task_id: 'w1',
    name: 'writer',
    agent_role: 'writer',
    status: 'completed',
    outputs: {
      latex_paper: '\\section{Intro}',
      ...(paper_version !== undefined ? { paper_version } : {}),
    },
  };
}

function paperQATask(status: PipelineTask['status']): PipelineTask {
  return {
    task_id: 'g1',
    name: 'paper_qa_gate',
    agent_role: 'orchestrator',
    status,
  };
}

beforeEach(() => {
  apiGetMock.mockReset();
  apiPostMock.mockReset();
  apiGetMock.mockResolvedValue({ messages: [] });
  apiPostMock.mockResolvedValue({ ok: true });
});

describe('usePaperSession', () => {
  it('returns null when run is null', () => {
    const { result } = renderHook(() => usePaperSession(null));
    expect(result.current).toBeNull();
  });

  it('yields mode=no-paper when pipeline has no writer output', () => {
    const run = makeRun({ pipeline: [] });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.mode).toBe('no-paper');
    expect(result.current?.hasPaper).toBe(false);
  });

  it('enters gate mode when paper_qa_gate awaits gate and writer has output', async () => {
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.mode).toBe('gate');
    expect(result.current?.isHistorical).toBe(false);
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith(
      '/api/runs/run-1/paper-qa/history',
    ));
  });

  it('onAccept posts no-action to the gate endpoint in gate mode', async () => {
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    const { result } = renderHook(() => usePaperSession(run));
    await act(async () => {
      await result.current!.onAccept();
    });
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/gate/paper_qa',
      { action: 'no', question: '' },
    );
  });

  it('returns isRewriting=true when theory task is in_progress after gate completion', () => {
    const run = makeRun({
      pipeline: [
        writerTask(2),
        paperQATask('completed'),
        {
          task_id: 't1', name: 'theory', agent_role: 'theory',
          status: 'in_progress',
        } as PipelineTask,
      ],
    });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.mode).toBe('rewriting');
    expect(result.current?.isRewriting).toBe(true);
  });

  it('onRewrite in gate mode POSTs to /rewrite (unified endpoint)', async () => {
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    const { result } = renderHook(() => usePaperSession(run));

    await act(async () => {
      await result.current!.onRewrite('retry proof');
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/rewrite',
      { revision_prompt: 'retry proof' },
    );
  });

  it('onRewrite in completed mode POSTs to /rewrite and appends optimistic marker', async () => {
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('completed')],
    });
    const { result } = renderHook(() => usePaperSession(run));

    await act(async () => {
      await result.current!.onRewrite('tighten Section 3');
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/rewrite',
      { revision_prompt: 'tighten Section 3' },
    );
    const sysMsg = result.current!.messages.find(
      (m) => m.role === 'system' && m.content.includes('tighten Section 3'),
    );
    expect(sysMsg).toBeDefined();
    expect(sysMsg!.content).toBe('↻ Rewrite requested: "tighten Section 3"');
  });

  it('paperVersion reads writer.outputs.paper_version when present', () => {
    const run = makeRun({ pipeline: [writerTask(3), paperQATask('completed')] });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.paperVersion).toBe(3);
  });

  it('paperVersion falls back to 1 + rewrite-marker count when writer lacks the field', async () => {
    const run = makeRun({
      pipeline: [
        {
          task_id: 'w1', name: 'writer', agent_role: 'writer',
          status: 'completed',
          outputs: { latex_paper: '\\section{X}' },
        } as PipelineTask,
        paperQATask('awaiting_gate'),
      ],
    });
    apiGetMock.mockResolvedValue({
      messages: [
        { role: 'system', content: '↻ Rewrite requested: "round 1"', ts: '2026-04-17T00:00:00Z' },
        { role: 'system', content: '↻ Rewrite requested: "round 2"', ts: '2026-04-17T01:00:00Z' },
      ],
    });
    const { result } = renderHook(() => usePaperSession(run));
    await waitFor(() => {
      expect(result.current?.paperVersion).toBe(3);
    });
  });

  it('mode transitions completed → rewriting when theory flips to in_progress', async () => {
    const completedRun = makeRun({
      pipeline: [writerTask(1), paperQATask('completed')],
    });
    const { result, rerender } = renderHook(
      ({ run }: { run: SessionRun }) => usePaperSession(run),
      { initialProps: { run: completedRun } },
    );
    await waitFor(() => expect(result.current?.mode).toBe('completed'));

    const rewritingRun = makeRun({
      pipeline: [
        writerTask(1),
        paperQATask('completed'),
        {
          task_id: 't1', name: 'theory', agent_role: 'theory',
          status: 'in_progress',
        } as PipelineTask,
      ],
    });
    rerender({ run: rewritingRun });
    expect(result.current?.mode).toBe('rewriting');
  });

  it('history load fires on run_id change', async () => {
    const run1 = makeRun({
      run_id: 'run-A',
      pipeline: [writerTask(1), paperQATask('completed')],
    });
    const { rerender } = renderHook(
      ({ run }: { run: SessionRun }) => usePaperSession(run),
      { initialProps: { run: run1 } },
    );
    await waitFor(() =>
      expect(apiGetMock).toHaveBeenCalledWith('/api/runs/run-A/paper-qa/history'),
    );

    const run2 = makeRun({
      run_id: 'run-B',
      pipeline: [writerTask(1), paperQATask('completed')],
    });
    rerender({ run: run2 });

    await waitFor(() =>
      expect(apiGetMock).toHaveBeenCalledWith('/api/runs/run-B/paper-qa/history'),
    );
  });

  it('history-load failure preserves optimistic rewrite markers', async () => {
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    apiPostMock.mockResolvedValue({ ok: true });
    apiGetMock.mockRejectedValue(new Error('network down'));
    const { result } = renderHook(() => usePaperSession(run));

    await act(async () => {
      await result.current!.onRewrite('keep me alive');
    });

    await waitFor(() => {
      const marker = result.current!.messages.find(
        (m) => m.role === 'system' && m.content.includes('keep me alive'),
      );
      expect(marker).toBeDefined();
    });
  });
});
