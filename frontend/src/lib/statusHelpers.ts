import type { SessionRun, PipelineTask } from '@/types';
import { titleCase, humanize } from './formatters';
import { STAGE_TASK_MAP, INNER_STAGE_LABELS } from './agentManifest';

export function statusClass(status: string): string {
  if (status === 'completed' || status === 'available') return 'status-complete';
  if (status === 'running' || status === 'in_progress' || status === 'configured') return 'status-active';
  if (status === 'failed' || status === 'missing') return 'status-error';
  if (status === 'paused') return 'status-paused';
  if (status === 'pausing') return 'status-pausing';
  if (status === 'resuming') return 'status-resuming';
  if (status === 'optional') return 'status-warning';
  return 'status-idle';
}

// Short meta used in the top-bar identity footer. Keeps the fact
// count down: status + active outer stage + elapsed (when relevant).
export function compactRunMeta(run: SessionRun | null, elapsedSeconds: number): string {
  if (!run) return '';
  if (run.status === 'queued') return 'Queued — waiting to start…';
  if (run.status === 'running') {
    const stage = getActiveOuterStage(run.pipeline ?? []);
    const label = stage ? titleCase(stage) : 'Running';
    return elapsedSeconds > 0 ? `${label} · ${formatElapsed(elapsedSeconds)}` : label;
  }
  if (run.status === 'completed') return 'Completed';
  if (run.status === 'paused') return 'Paused — resume when ready';
  if (run.status === 'pausing') return 'Stopping safely…';
  if (run.status === 'resuming') return 'Resuming…';
  if (run.status === 'failed') return 'Failed';
  return titleCase(run.status);
}

function formatElapsed(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

export function getActiveOuterStage(pipeline: PipelineTask[]): string | null {
  if (!pipeline || !pipeline.length) return null;
  const running = pipeline.find((t) => t.status === 'in_progress' || t.status === 'running');
  if (running) return STAGE_TASK_MAP[running.name] ?? null;
  const done = pipeline.filter((t) => t.status === 'completed');
  if (done.length) return STAGE_TASK_MAP[done[done.length - 1].name] ?? null;
  return null;
}

export function friendlyInnerStage(rawStage: string): string | null {
  if (!rawStage) return null;
  return (
    INNER_STAGE_LABELS[rawStage] ||
    humanize(rawStage)
      .replace(/Agent$/, '')
      .replace(/([A-Z])/g, ' $1')
      .trim()
      .toLowerCase()
  );
}
