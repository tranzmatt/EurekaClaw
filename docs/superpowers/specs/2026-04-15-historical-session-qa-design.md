# Historical Session QA — Design Spec

## Overview

Enable users to revisit completed sessions, review generated papers (PDF/LaTeX), ask QA questions, and trigger rewrites — the same Paper QA Gate experience, but for historical sessions. Works in both CLI and UI.

## Problem

Completed sessions have `paper_qa_gate` status `completed`. The PaperReviewPanel only renders when status is `awaiting_gate`. Users cannot ask questions about or request rewrites of papers from past sessions. All session artifacts are persisted to disk (`~/.eurekaclaw/runs/{session_id}/`) but there is no way to load them back into an interactive review.

## Goals

1. Users can review any completed session's paper via CLI (`eurekaclaw review <session_id>`) or UI ("Review Paper" button)
2. Full QA capability: multi-turn questions with tool-equipped PaperQAAgent
3. Full rewrite capability: re-run theory + writer with feedback, even for old sessions
4. QA history appended to the original session's `paper_qa_history.jsonl`
5. Rewrite updates the original session's paper artifacts on disk
6. Reuse existing `PaperQAHandler` — no new gate logic

## Non-Goals

- Creating separate child sessions for historical QA (decided: mutate original)
- Browsing/searching sessions by content (just list by date)
- Replaying the full pipeline (only theory + writer for rewrites)

## Architecture

### Session Reconstruction

New module `eurekaclaw/orchestrator/session_loader.py` reconstructs a runnable session from persisted artifacts:

```
SessionLoader.load(session_id) -> (KnowledgeBus, ResearchBrief, TaskPipeline)
```

Steps:
1. Find session dir: `~/.eurekaclaw/runs/{session_id}/`
2. Load bus via `KnowledgeBus.load(session_id, session_dir)`
3. Extract `latex_paper` from pipeline's writer task outputs or from `{output_dir}/paper.tex`
4. Put latex on bus as `paper_qa_latex`
5. Return `(bus, brief, pipeline)`

The caller (CLI command or server endpoint) creates a `MetaOrchestrator` from the loaded bus when rewrite is needed. For QA-only, only the bus and a `PaperQAAgent` are needed.

### CLI Commands

#### `eurekaclaw sessions`

Lists all persisted sessions from `~/.eurekaclaw/runs/`.

Output (Rich table):
```
 # | Session ID  | Domain           | Query                      | Status    | Date
---+-------------+------------------+----------------------------+-----------+-----------
 1 | 0a370c0a... | spectral_graph   | Convergence of spectral... | completed | 2026-04-12
 2 | 0512e55e... | machine_learning | Prove generalization...    | completed | 2026-04-10
```

Implementation:
- Scan `~/.eurekaclaw/runs/*/research_brief.json`
- Parse each brief for domain, query, session_id
- Check pipeline.json for status (last task status)
- Sort by directory modification time (most recent first)
- Display via Rich Table

#### `eurekaclaw review <session_id>`

Loads a historical session and enters the QA/rewrite loop.

- Accepts full or partial session IDs (prefix match, minimum 8 chars)
- Calls `SessionLoader.load()` to reconstruct bus
- Creates `PaperQAHandler` with loaded artifacts
- Skips `_should_review()` prompt (user explicitly chose to review)
- Enters `_review_loop()` directly
- On rewrite: creates fresh `MetaOrchestrator` to re-run theory + writer
- If no `latex_paper` found: prints error and exits

Implementation location: new subcommands in `eurekaclaw/cli.py`

### Frontend Changes

#### PaperPanel — "Review Paper" button

In the existing `PaperPanel` component, add a button when:
- `run.status === 'completed'`
- `run.result?.latex_paper` or writer task outputs contain LaTeX

Button: `"Review Paper"` (primary style), calls `POST /api/runs/{run_id}/review` then sets `reviewSessionId` in UI store.

#### UI Store — reviewSessionId

Add to `uiStore`:
```typescript
reviewSessionId: string | null;
setReviewSessionId: (id: string | null) => void;
```

#### WorkspaceTabs — conditional rendering

Update the panel activation condition:
```typescript
const reviewModeActive = reviewSessionId === run?.run_id;
const isReviewActive = isGateActive || isRewriteRunning || reviewModeActive;
```

When `reviewModeActive` is true but `isGateActive` is false (historical session), PaperReviewPanel renders with:
- "Close Review" button instead of "Accept Paper"
- Clicking "Close Review" clears `reviewSessionId`

#### PaperReviewPanel — historical mode awareness

The panel checks whether it's in live gate mode or historical review mode:
```typescript
const isHistorical = !isGateActive && reviewSessionId === run.run_id;
```

Differences in historical mode:
- "Accept Paper" button label changes to "Close Review"
- "Close Review" calls `setReviewSessionId(null)` instead of submitting gate
- "Rewrite Paper" calls `POST /api/runs/{run_id}/review/rewrite` instead of gate submission
- QA questions still use `POST /api/runs/{run_id}/paper-qa/ask` (same endpoint)

### Backend Endpoints

#### `POST /api/runs/{run_id}/review`

Activates review mode for a completed session.

1. Load bus from disk via `SessionLoader.load()`
2. Attach loaded bus to the run object (`run.eureka_session` or a new field)
3. Put `paper_qa_latex` on the bus
4. Return `{ "ok": true }`

After this call, `/paper-qa/ask` and `/paper-qa/history` endpoints work because the bus is populated.

#### `POST /api/runs/{run_id}/review/rewrite`

Triggers theory + writer re-run for a historical session.

Request:
```json
{ "revision_prompt": "Tighten the bound in Theorem 2..." }
```

Implementation:
1. Load bus from disk (or use already-loaded bus from review activation)
2. Create fresh `MetaOrchestrator` with current settings
3. Build feedback from QA history + revision prompt
4. Reset theory + writer tasks in pipeline, inject feedback
5. Execute theory then writer
6. Persist updated artifacts to disk (bus.persist + save paper_v{n}.tex)
7. Return `{ "ok": true, "latex_paper": "..." }`

On failure: return error, original artifacts unchanged.

## Data Flow

```
User clicks "Review Paper" (UI) or runs `eurekaclaw review` (CLI)
    |
    v
SessionLoader.load(session_id)
    |-- reads ~/.eurekaclaw/runs/{session_id}/*.json
    |-- reconstructs KnowledgeBus with brief, theory_state, bibliography, pipeline
    |-- extracts latex_paper from writer task outputs or paper.tex file
    |-- puts paper_qa_latex on bus
    |
    v
PaperReviewPanel (UI) or _review_loop (CLI)
    |
    |-- QA questions --> PaperQAAgent.ask() --> answer
    |   |-- history appended to paper_qa_history.jsonl in original session dir
    |
    |-- Rewrite --> MetaOrchestrator (fresh) --> theory + writer re-run
    |   |-- updated artifacts persisted to original session dir
    |   |-- paper_v{n}.tex saved
    |   |-- returns to review loop with new paper
    |
    |-- Accept/Close --> exit review
```

## File Changes

### New Files

| File | Purpose |
|------|---------|
| `eurekaclaw/orchestrator/session_loader.py` | Reconstruct bus + artifacts from disk |

### Modified Files

| File | Change |
|------|--------|
| `eurekaclaw/cli.py` | Add `sessions` and `review` subcommands |
| `eurekaclaw/orchestrator/paper_qa_handler.py` | Add `from_session_id()` classmethod for CLI review; add `run_historical()` method that skips `_should_review()` |
| `eurekaclaw/ui/server.py` | Add `POST /api/runs/{run_id}/review` and `POST /api/runs/{run_id}/review/rewrite` endpoints |
| `frontend/src/components/workspace/PaperPanel.tsx` | Add "Review Paper" button for completed sessions |
| `frontend/src/components/workspace/WorkspaceTabs.tsx` | Add `reviewModeActive` condition |
| `frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx` | Handle historical mode (Close Review, rewrite via review endpoint) |
| `frontend/src/components/workspace/paper-review/QAChat.tsx` | Conditional button label and action based on historical mode |
| `frontend/src/store/uiStore.ts` | Add `reviewSessionId` state |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Session ID not found | CLI: "Session not found" error. API: 404 |
| No latex_paper in session | CLI: "No paper found in session" error. UI: "Review Paper" button hidden |
| Partial session ID ambiguous | CLI: show matching sessions, ask user to be more specific |
| Rewrite fails (theory/writer crash) | Rollback to previous paper version, same as live gate behavior |
| Session artifacts corrupted | CLI: error message. API: 500 with description |

## Testing Strategy

- Unit test for `SessionLoader.load()` with mock session dir
- Unit test for `eurekaclaw sessions` output formatting
- Unit test for partial session ID matching
- Unit test for PaperReviewPanel historical mode props
- Integration: load real session, ask QA question, verify history persisted
