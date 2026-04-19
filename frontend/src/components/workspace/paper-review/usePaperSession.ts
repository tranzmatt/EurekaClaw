import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost } from '@/api/client';
import { REWRITE_MARKER_PREFIX } from '@/constants/paper';
import type { SessionRun, QAMessage } from '@/types';

export type PaperMode =
  | 'no-paper'
  | 'gate'
  | 'rewriting'
  | 'completed'
  | 'failed';

export interface PaperSession {
  mode: PaperMode;
  run: SessionRun;

  hasPaper: boolean;
  paperVersion: number;
  latexSource: string;

  messages: QAMessage[];
  setMessages: React.Dispatch<React.SetStateAction<QAMessage[]>>;

  isRewriting: boolean;
  theoryStatus: string;
  writerStatus: string;

  onAccept: () => Promise<void>;
  onRewrite: (prompt: string) => Promise<void>;

  isHistorical: boolean;
}

type HistoryResponse = { messages: QAMessage[] };

export function usePaperSession(run: SessionRun | null): PaperSession | null {
  const [messages, setMessages] = useState<QAMessage[]>([]);
  const [isRewriting, setIsRewriting] = useState(false);

  const writerTask = run?.pipeline?.find((t) => t.name === 'writer');
  const theoryTask = run?.pipeline?.find((t) => t.name === 'theory');
  const paperQATask = run?.pipeline?.find((t) => t.name === 'paper_qa_gate');

  const latexSource = useMemo(() => {
    if (writerTask?.outputs?.latex_paper) {
      return String(writerTask.outputs.latex_paper);
    }
    if (run?.result?.latex_paper) {
      return run.result.latex_paper;
    }
    return '';
  }, [writerTask?.outputs?.latex_paper, run?.result?.latex_paper]);

  const hasPaper = latexSource.length > 0;

  const pipelineRewriting =
    theoryTask?.status === 'in_progress' ||
    theoryTask?.status === 'pending' ||
    writerTask?.status === 'in_progress' ||
    writerTask?.status === 'pending';

  const mode: PaperMode = useMemo(() => {
    if (!run) return 'no-paper';
    if (run.status === 'failed' && !hasPaper) return 'failed';
    if (!hasPaper) return 'no-paper';
    if (paperQATask?.status === 'awaiting_gate') return 'gate';
    if (pipelineRewriting) return 'rewriting';
    return 'completed';
  }, [run, hasPaper, paperQATask?.status, pipelineRewriting]);

  useEffect(() => {
    if (!run?.run_id) return;
    void (async () => {
      try {
        const data = await apiGet<HistoryResponse>(
          `/api/runs/${run.run_id}/paper-qa/history`,
        );
        const serverMsgs = data.messages ?? [];
        setMessages((prev) => {
          const serverKeys = new Set(
            serverMsgs.map((m) => `${m.role}|${m.content}`),
          );
          const optimistic = prev.filter(
            (m) =>
              m.role === 'system' &&
              typeof m.content === 'string' &&
              m.content.startsWith(REWRITE_MARKER_PREFIX) &&
              !serverKeys.has(`${m.role}|${m.content}`),
          );
          return [...serverMsgs, ...optimistic];
        });
      } catch {
        setMessages((prev) =>
          prev.filter(
            (m) =>
              m.role === 'system' &&
              typeof m.content === 'string' &&
              m.content.startsWith(REWRITE_MARKER_PREFIX),
          ),
        );
      }
    })();
  }, [run?.run_id, mode]);

  const isHistorical = mode !== 'gate';

  const onAccept = useCallback(async () => {
    if (!run?.run_id) return;
    if (mode !== 'gate') return;
    await apiPost(`/api/runs/${run.run_id}/gate/paper_qa`, {
      action: 'no',
      question: '',
    });
  }, [run?.run_id, mode]);

  const onRewrite = useCallback(
    async (prompt: string) => {
      if (!run?.run_id) return;
      const marker: QAMessage = {
        role: 'system',
        content: `${REWRITE_MARKER_PREFIX}"${prompt}"`,
        ts: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, marker]);
      setIsRewriting(true);
      try {
        await apiPost(`/api/runs/${run.run_id}/rewrite`, {
          revision_prompt: prompt,
        });
      } catch (e) {
        const errMsg: QAMessage = {
          role: 'system',
          content: `Revision error: ${e instanceof Error ? e.message : String(e)}`,
          ts: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, errMsg]);
      } finally {
        setIsRewriting(false);
      }
    },
    [run?.run_id],
  );

  const paperVersion = useMemo(() => {
    const fromOutputs = writerTask?.outputs?.paper_version;
    if (typeof fromOutputs === 'number' && fromOutputs > 0) {
      return fromOutputs;
    }
    const markerCount = messages.filter(
      (m) =>
        m.role === 'system' &&
        typeof m.content === 'string' &&
        m.content.startsWith(REWRITE_MARKER_PREFIX),
    ).length;
    return 1 + markerCount;
  }, [writerTask?.outputs?.paper_version, messages]);

  if (!run) return null;

  return {
    mode,
    run,
    hasPaper,
    paperVersion,
    latexSource,
    messages,
    setMessages,
    isRewriting: isRewriting || !!pipelineRewriting,
    theoryStatus: theoryTask?.status ?? 'pending',
    writerStatus: writerTask?.status ?? 'pending',
    onAccept,
    onRewrite,
    isHistorical,
  };
}
