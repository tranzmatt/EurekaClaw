# Paper Session Redesign — Design Spec

**Branch**: `shiyuan/paper-qa-gate`
**Date**: 2026-04-17
**Author**: Chenggong Zhang (with Claude)

## Problem

The Paper tab's feature/logic order feels tangled after a long series of bug
fixes. Two near-duplicate React panels (`PaperPanel`, `PaperReviewPanel`) own
overlapping responsibilities with subtly different state machines, rewrite
dispatch, divider behavior, and paper-version derivation. Mode transitions
(gate → rewriting → completed) can lose state or flash stale badges. A recent
PDF-delete-on-GET bug and the rewrite-marker-persistence saga both traced back
to responsibilities being split across files that should have been one.

## Goals

1. Collapse the two Paper panels into one entry point backed by a single
   state-machine hook.
2. Preserve every feature currently working: PDF/LaTeX viewer, QA chat, Accept
   during gate, Revise/rewrite, history persistence, rewrite markers, session
   switching, auto-compile, downloads.
3. Do not open the backend endpoint-consolidation can of worms — keep the
   backend cleanup "small": only what the frontend collapse actually requires,
   plus two tiny dedup/constant wins.
4. No behavioral regressions on the five recent fix classes: (i) PDF compile
   produces a PDF, (ii) iframe loads the PDF, (iii) rewrite markers persist
   only on success, (iv) QA history survives refresh and folds system→user,
   (v) gate-to-completed transition preserves messages.

## Non-Goals

- Merging `/review/rewrite` and `/gate/paper_qa` into a single endpoint.
- Removing the `/api/runs/<id>/review` bus-activation step (lazy bus load).
- Changing `_stub_missing_sections` / `_fix_missing_citations` behavior.
- Overhauling the live-session Live/Proof tabs.

## Architecture

### Frontend file layout

```
frontend/src/components/workspace/
├── PaperPanel.tsx                 # single entry, mounted from WorkspaceTabs
└── paper-review/
    ├── usePaperSession.ts         # NEW: state-machine hook
    ├── usePaperSession.test.ts    # NEW: vitest suite
    ├── PaperViewer.tsx            # unchanged, pure presentation
    ├── QAChat.tsx                 # unchanged, pure presentation
    └── ChatMessage.tsx            # unchanged
```

Delete: `paper-review/PaperReviewPanel.tsx`.

### WorkspaceTabs change

- Remove the `isGateActive / isRewriteRunning` fork and the panel-takeover
  branch (`WorkspaceTabs.tsx:24-45`).
- Paper tab always renders `PaperPanel`. Tab bar stays visible in all modes so
  the user can freely switch between Live/Proof/Paper even during an active
  gate or rewrite.

### PaperPanel responsibilities

- Single job: pick a render branch based on `session.mode`:
  - `'no-paper'` → empty state ("Launch a session…" / "Paper will appear once
    the writer agent completes.")
  - `'failed'` → error state (surfaces `run.error`)
  - `'loading-review'` → spinner ("Loading paper review…")
  - every other mode → left `PaperViewer` + draggable divider + right
    `QAChat`.
- All business state comes from `usePaperSession(run)`.
- `key={run.run_id}` lives on this component so session switches remount the
  hook.

### PaperViewer / QAChat

- Prop shapes unchanged to avoid cascading changes to their styling or any
  existing tests.
- `QAChat` keeps receiving `isHistorical` (now computed inside the hook) to
  decide whether to show the Accept button.

### Divider

- Single draggable divider with localStorage persistence (the existing
  `PaperReviewPanel` behavior, clamped `[30, 70]%`, key
  `eurekaclaw-review-split`). Static divider removed.

## `usePaperSession(run)` hook

### Interface

```ts
type PaperMode =
  | 'no-paper'
  | 'loading-review'
  | 'gate'
  | 'rewriting'
  | 'completed'
  | 'failed';

interface PaperSession {
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

  reviewError: string | null;
  isHistorical: boolean;
}

export function usePaperSession(run: SessionRun | null): PaperSession | null;
```

Returns `null` only when `run === null`.

### Mode derivation (pure)

`run === null` is handled before reaching this logic (the hook returns
`null`). Given a non-null `run`, apply the first matching branch:

```
run.status === 'failed' && !hasPaper              → 'failed'
!hasPaper                                         → 'no-paper'
paperQATask.status === 'awaiting_gate'            → 'gate'
paperQATask.completed &&
  (theory|writer running/pending/in_progress)     → 'rewriting'
reviewStatus !== 'ready'                          → 'loading-review'
otherwise                                         → 'completed'
```

`reviewStatus` is internal state (`'idle' | 'loading' | 'ready' | 'failed'`),
not exposed on the returned object; it's reflected into `reviewError` (when
failed) and `mode === 'loading-review'` (when loading).

### Effects

1. **Review activation** — runs when `mode === 'completed'` (or its
   pre-ready `'loading-review'`) and `reviewStatus === 'idle'`.
   POSTs `/api/runs/<id>/review`, transitions
   `idle → loading → ready | failed`. Skipped for `'gate'` / `'rewriting'`
   because the bus is already in-memory.
2. **History load** — runs when `reviewStatus === 'ready'` OR
   `mode in ('gate', 'rewriting')`. GETs
   `/api/runs/<id>/paper-qa/history`, `setMessages(data.messages)`.
3. **Session reset** — not done inside the hook; the caller mounts with
   `key={run.run_id}` so `run_id` change = fresh hook instance.

### `onAccept()`

- `mode === 'gate'`: POST `/api/runs/<id>/gate/paper_qa` body
  `{action: 'no', question: ''}`.
- otherwise: resolve immediately (no-op); `QAChat` hides the Accept button
  via `isHistorical` anyway.

### `onRewrite(prompt)`

- Append optimistic system message using `REWRITE_MARKER_PREFIX`
  (`↻ Rewrite requested: "<prompt>"`) to `messages`.
- `mode === 'gate'`: POST `/gate/paper_qa` `{action: 'rewrite', question}`
  (returns quickly; orchestrator runs the rewrite async).
- otherwise: POST `/review/rewrite` `{revision_prompt}`
  (blocks until theory+writer finish or fail).
- On POST 200: set `reviewStatus = 'idle'`. Safe to do immediately —
  the bus-reload effect is gated on `mode === 'completed'`, so it won't
  re-fire during `'rewriting'`. When mode returns to `'completed'` after
  the rewrite finishes, the effect re-runs and pulls fresh bus + history.
- On POST failure: append system error message. Do not rollback the
  optimistic marker — the next `reviewStatus = 'idle'` + history GET
  will overwrite `messages` with the authoritative server copy, which
  omits the marker on failed/rejected rewrites.

### `paperVersion`

- Primary: `writerTask.outputs.paper_version` if present (an int, backend-
  authoritative).
- Fallback (for runs produced before this change):
  `1 + messages.filter(m => m.role==='system' && m.content.startsWith(REWRITE_MARKER_PREFIX)).length`.

### `reviewError`

- `null` unless `reviewStatus === 'failed'`, then the error text from the
  failed POST. `PaperPanel` renders a non-blocking banner above `PaperViewer`
  and omits `QAChat` while this is set.

### `isHistorical`

- `true` when `mode !== 'gate'`. Surfaced to `QAChat` so the Accept button
  shows only during an active gate.

## Backend changes (small)

### (A) `paper_version` field — required by frontend hook

- **Writer agent** (`eurekaclaw/agents/writer/agent.py`): when writing
  outputs for the first time, set `outputs['paper_version'] = 1`.
- **PaperQAHandler** (`eurekaclaw/orchestrator/paper_qa_handler.py`): inside
  the gate-path `if new_latex is not None:` branch, read current
  `writer.outputs.paper_version` (default 1) and bump to `+1` before writing
  updated outputs.
- **`/review/rewrite`** (`eurekaclaw/ui/server.py:~2087`): after a successful
  `handler._do_rewrite`, apply the same bump to the writer task outputs in
  the run's pipeline.

### (B) `_sync_latex_to_disk(run)` helper

Extract to `eurekaclaw/ui/server.py` module scope:

```python
def _sync_latex_to_disk(run) -> tuple[bool, str]:
    """Sync writer bus latex to run.output_dir/paper.tex.

    Returns (changed, latex). Writes paper.tex only if bus latex differs
    from the on-disk copy. Never touches paper.pdf — callers are
    responsible for PDF lifecycle.
    """
```

Replace the duplicated bus-sync logic in:
- `/compile-pdf` handler (server.py:~1851-1859). This handler ALSO deletes
  the stale PDF when `changed == True` — the helper does not do that, the
  caller does.
- `/artifacts/paper.tex` handler (server.py:~1605-1624, after the recent
  PDF fix scoped this to `.tex`). This handler does not delete anything.

### (C) `REWRITE_MARKER_PREFIX` constant

- Python side: add to `eurekaclaw/ui/constants.py` (create if absent) as
  `REWRITE_MARKER_PREFIX = '↻ Rewrite requested: '`.
- TypeScript side: add to `frontend/src/constants/paper.ts` with the same
  value.
- Replace the three open-coded string constructions:
  - `PaperPanel.tsx:70` (will be removed; move to hook's `onRewrite`).
  - `server.py:_append_paper_qa_rewrite_marker` (~line 2319).
  - `paper_qa_handler.py` gate-rewrite-success branch (~line 180).

## Data flow

### Mode transitions (happy path)

```
mount
  → no-paper
  → (writer finishes) gate
  → (user Accept) completed → loading-review → completed (historical)
  | (user Rewrite) rewriting → (writer finishes) completed
```

### Session switch

Parent `key={run.run_id}` remounts `PaperPanel`, which remounts the hook.
All state resets cleanly; no `useEffect` cleanup juggling required.

### Gate-period tab switch

User clicks Live → Proof → back to Paper while gate is active. `PaperPanel`
unmounts and remounts only if `WorkspaceTabs` stops rendering it — with the
takeover branch removed, `PaperPanel` stays in the DOM (it's conditionally
visible via `ws-panel` CSS), so hook state and messages are preserved.

## Error handling

| Source | Behavior |
|---|---|
| Review activation 500 | `reviewError` set; `PaperPanel` renders banner above `PaperViewer`; no `QAChat`. |
| `/compile-pdf` 500 | Existing paper.log tail logic preserved. |
| `/paper-qa/ask` 500 | `QAChat` appends assistant error bubble (unchanged). |
| `/review/rewrite` 500 | `QAChat` appends system error; backend already rolls back via backup. |
| `/gate/paper_qa` 500 | Same as `/review/rewrite` (system error message, no `alert()`). |
| Network error on any of the above | Caught by `apiPost`, surfaces via same channel as the 500 case. |

## Invariants the hook must preserve

1. Mode change never clears `messages`. Only `run.run_id` change does, via
   the remount.
2. `setMessages` is stable (wrapped in `useCallback` or the plain state
   setter) so `QAChat`'s `useEffect([messages])` doesn't thrash.
3. `isRewriting` is derived from `run.pipeline`, not from internal state —
   polling updates to `run` drive the spinner.
4. `paperVersion` monotonically increases within a single `run_id` session.
5. The "Compiled" badge in `PaperViewer` requires
   `pdfAvailable && !isRewriting && mode === 'completed'`; never shows
   during rewriting.

## Testing

### Frontend (new vitest suite)

`frontend/src/components/workspace/paper-review/usePaperSession.test.ts`
using `@testing-library/react` `renderHook` + `vi.fn()` fetch mocks:

1. `no-paper` mode: empty pipeline yields `mode === 'no-paper'`, actions
   are safe no-ops.
2. `gate` mode: `paper_qa_gate awaiting_gate` yields `mode === 'gate'`;
   `onAccept()` POSTs `/gate/paper_qa` with `{action: 'no'}`.
3. `rewriting` mode: theory `in_progress` yields `isRewriting === true`.
4. `completed` mode: POST `/review` resolves → `mode` transitions through
   `loading-review` to `completed`; GET `/paper-qa/history` messages
   populate `messages`.
5. `onRewrite` in gate vs completed: verifies correct endpoint (via mock)
   is called; frontend caller (the test simulating `QAChat`) sees one
   function, no branching.
6. Optimistic marker: `onRewrite('fix X')` synchronously appends a system
   message with content `↻ Rewrite requested: "fix X"`.
7. `paperVersion`: when `writer.outputs.paper_version === 3`, hook returns
   `3`; when absent, hook falls back to `1 + rewrite-marker count`.

### Backend (new and extended pytest)

- Extend `tests/unit/test_paper_qa_handler.py`: `_do_rewrite` returning
  non-None results in writer outputs `paper_version` incrementing.
- New `tests/unit/test_paper_version.py`: fresh writer run sets
  `paper_version = 1`; `/review/rewrite` success bumps; failed rewrite
  does not bump.
- New `tests/unit/test_sync_latex_to_disk.py`: bus latex equal to disk
  returns `(False, latex)`; differing writes new content; paper.pdf
  never unlinked.

### Manual verification (post-implementation)

- [ ] Fresh session → gate → QA → Rewrite → new version → Accept → gate
      closes.
- [ ] Browser refresh in each of `gate` / `rewriting` / `completed`
      restores messages and PDF.
- [ ] Session switch resets state cleanly.
- [ ] Gate-period switch to Live, back to Paper: messages preserved.
- [ ] PDF compile failure surfaces paper.log tail.
- [ ] Failed session renders the failure state without crashing.
- [ ] Old runs (with no `paper_version` field) still render with fallback
      version counting.

## Rollout / Implementation order

One commit per step, in this order:

1. Backend (A): add `paper_version` in writer agent, orchestrator handler,
   `/review/rewrite` success branch. Unit tests.
2. Backend (B), (C): extract `_sync_latex_to_disk` helper and
   `REWRITE_MARKER_PREFIX` constant. Unit tests.
3. Frontend hook: add `usePaperSession` + vitest suite. Not wired up.
4. Frontend merge: convert `PaperPanel` to the hook; delete
   `PaperReviewPanel.tsx`; remove the fork in `WorkspaceTabs.tsx`.
5. Manual verification against the checklist above.

Each step is revertable in isolation. If step 4 reveals a hook design
issue, steps 1–3 remain on main untouched.

## Open questions

None at this stage. The remaining judgment calls (endpoint consolidation,
lazy bus load) are explicitly deferred as non-goals.
