# Paper Tab: Unified Ready State — Design Spec

**Date:** 2026-04-17
**Branch:** `shiyuan/paper-qa-gate`
**Supersedes:** `docs/superpowers/specs/2026-04-17-paper-rewrite-unification-design.md` (commit `76530e2`). That spec's scope (unifying `/review*` and `/rewrite` endpoints, adding `_ensure_bus_activated`, including experiment in `_do_rewrite`) is preserved here. This spec extends further: it deletes the `paper_qa_gate` pipeline stage, removes the Accept affordance, unifies Ask and Rewrite into a single chat, and collapses all post-writer UI states into one `ready` state.

---

## 1. Problem

The current Paper tab has three user complaints:

1. **PDF fails to render on a new session.** Opening the Paper tab after the writer agent finishes shows `"Error: pdflatex ran but produced no PDF — check paper.log"`. Only after clicking the Accept button does the PDF render. Root cause: `PaperViewer.tsx` auto-fires `POST /compile-pdf` on tab mount, `/compile-pdf` reads `paper.tex` from disk via `_sync_latex_to_disk(run)`, but `run.eureka_session.bus` is populated through an async path that can race the first compile. A second compile (triggered after Accept) succeeds because the bus has caught up.

2. **The three-affordance UX (Ask / Accept / Rewrite) is cumbersome.** Accept is the dominant visible action but is semantically a no-op — it just dismisses the gate. Users want to read the paper and, optionally, ask a question or request a rewrite. A dismiss-only button on the critical path is noise.

3. **Backend duplication.** Five UI-facing endpoints currently handle paper-session state (`/paper-qa/ask`, `/paper-qa/history`, `/gate/paper_qa`, `/review`, `/review/rewrite`), with three of them duplicating "hydrate bus from disk for historical runs" logic and two of them (`/review/rewrite`, `/gate/paper_qa rewrite action`) duplicating the rewrite-dispatch logic. `_run_rewrite_bg` in `server.py` additionally inlines the same "write paper.tex + bump paper_version + persist bus" block that `_sync_latex_to_disk` already implements.

---

## 2. Design Goal

**One state once the paper exists: `ready`.** New sessions and historical completed sessions are indistinguishable in the UI. The two actions available in `ready` are Ask (question about the paper) and Rewrite (revise the paper). Both are available from the moment the paper PDF renders, regardless of how the user arrived there.

**Backend invariant:** whenever the writer agent completes, `paper.tex` exists on disk, `bus.writer_output.paper_version` is bumped, and the stale `paper.pdf` (if any) is gone. This is enforced at the pipeline layer — a single hook — not by the UI triggering a compile.

**Consolidation invariant:** each responsibility lives in exactly one function. Bus hydration for historical runs → `_ensure_bus_activated`. Post-writer artifact housekeeping → `on_writer_complete`. Rewrite dispatch → `_do_rewrite`. UI endpoints call these; they do not reimplement them.

---

## 3. Frontend Changes

### 3.1 `usePaperSession.ts` simplification

**Current mode enum:**
```ts
type PaperMode =
  | 'no-paper' | 'loading-review' | 'gate'
  | 'rewriting' | 'completed' | 'failed';
```

**New mode enum:**
```ts
type PaperMode = 'no-paper' | 'ready' | 'rewriting';
```

- `no-paper`: writer has not produced `latex_paper` yet (pipeline running or pre-writer). Covers the previous `failed` case too — if the run failed before the writer, no paper exists, so the state is `no-paper` with the run error shown as an empty-state message.
- `ready`: paper exists, no active rewrite. The **only** interactive state. Ask and Rewrite both available. This is the unified target of all post-writer flows.
- `rewriting`: a rewrite is in flight — theory or writer task is `pending | in_progress | running`. The viewer shows a "rewriting" overlay; chat is read-only.

**Deleted state and callbacks:**
- `reviewStatus` state (`'idle' | 'loading' | 'ready' | 'failed'`) — gone.
- `reviewError` state — gone.
- `isHistorical` derived flag — gone. The hook cannot distinguish new from historical runs, and that is the design goal.
- `onAccept` callback — gone.
- Effect 1 (the `POST /review` activation effect, lines 91–106) — gone.

**New callbacks:**
- `onAsk(question: string): Promise<void>` — POSTs `/api/runs/{run_id}/paper-qa/ask` with `{question}`. Appends an optimistic user message to the chat before the request, and relies on the server echo (via `/paper-qa/history` reload or the existing bus-backed response) to show the assistant reply.
- `onRewrite(prompt: string): Promise<void>` — POSTs `/api/runs/{run_id}/rewrite` with `{revision_prompt}`. Appends an optimistic rewrite marker (`↻ Rewrite requested: "…"`) to the chat. Does not block; returns immediately. The pipeline's own transition into `rewriting` mode (theory/writer back to `in_progress`) is what the UI watches for progress.

**Retained behavior:**
- Effect 2 (history load) still fires, but the trigger simplifies: `if (run?.run_id && hasPaper) fetch history`. No more `reviewStatus === 'ready' || mode === 'gate' || mode === 'rewriting'` gating.
- `paperVersion` computation (writer.outputs.paper_version, or fallback to `1 + marker count`) is unchanged.
- Optimistic-marker preservation on history-reload failure is unchanged.

### 3.2 `QAChat.tsx` → `PaperChat.tsx`

Rename and simplify. New props:

```ts
interface PaperChatProps {
  runId: string;
  messages: QAMessage[];
  setMessages: React.Dispatch<React.SetStateAction<QAMessage[]>>;
  disabled: boolean;              // true during 'rewriting'
  onAsk: (question: string) => Promise<void>;
  onRewrite: (prompt: string) => Promise<void>;
}
```

- **Accept button: deleted.** The component no longer takes `onAccept` or `isHistorical`.
- **Input bar:** a single textarea. Two buttons beside it: **Ask** (blue, default) and **Rewrite** (amber, secondary). Submitting a question with Ask calls `onAsk(text)`; submitting with Rewrite calls `onRewrite(text)`. Both clear the textarea on send.
- **Historical distinction removed:** the UI renders identically whether the run just finished or was opened from the sessions list. `disabled` is the only state that changes the input (and only during an active rewrite).

### 3.3 `PaperPanel.tsx` simplification

- Delete the `session.mode === 'loading-review'` branch (no more loading state).
- Delete the `session.mode === 'failed'` branch (folded into `no-paper`).
- Delete the `reviewError` banner (`session.reviewError` is gone).
- `no-paper` branch: if `run.status === 'running'`, show "Paper will appear once the writer agent completes"; else show `run.error || 'No paper generated yet.'`. One branch, both cases.
- The main layout (split viewer + chat) is only rendered when `session.mode === 'ready' || 'rewriting'`.

---

## 4. Backend Changes

### 4.1 `/compile-pdf` self-heal

Current behavior (`server.py:1897-1943`):
```python
# Always read paper.tex from disk. If it's missing → fail.
```

New behavior: if `paper.tex` is missing but `bus.get_writer_output().latex_paper` exists, write `paper.tex` from the bus before compiling. Belt-and-suspenders with the writer-complete hook — under normal flow the hook has already written the file, but the self-heal means a race or a bus-only session still compiles successfully.

```python
paper_tex = run_dir / "paper.tex"
if not paper_tex.exists():
    latex = bus.get_writer_output()
    if latex:
        paper_tex.write_text(latex, encoding="utf-8")
    else:
        return {"error": "no latex available"}, 400
# proceed to pdflatex invocation
```

### 4.2 `_ensure_bus_activated` helper

New module-level helper in `server.py`. Replaces the ad-hoc bus-hydration logic currently scattered across `/paper-qa/ask`, `/paper-qa/history`, and the deleted `/review` endpoint.

```python
def _ensure_bus_activated(run) -> tuple[KnowledgeBus, TaskPipeline, ResearchBrief]:
    """Return (bus, pipeline, brief) for a run.

    - If the run's EurekaSession is live and its bus has a pipeline,
      return the in-memory bus + its pipeline + its brief.
    - Otherwise hydrate from disk via SessionLoader.load(session_id),
      which reads {session_dir}/pipeline.json, research_brief.json, etc.
    """
    session = run.eureka_session
    if session and session.bus and session.bus.get_pipeline() is not None:
        bus = session.bus
        return bus, bus.get_pipeline(), bus.get_research_brief()
    # Historical or stalled run — hydrate from disk.
    from eurekaclaw.orchestrator.session_loader import SessionLoader
    bus, brief, pipeline = SessionLoader.load(run.eureka_session_id)
    return bus, pipeline, brief
```

### 4.3 `/paper-qa/ask` uses the helper

Current handler self-hydrates inline. Replace with:

```python
@app.post("/api/runs/{run_id}/paper-qa/ask")
async def paper_qa_ask(run_id: str, body: AskBody):
    run = _require_run(run_id)
    bus, pipeline, brief = _ensure_bus_activated(run)
    ... # proceed with existing ask logic using bus/pipeline/brief
```

### 4.4 `/paper-qa/history` reads disk-first

Already reads JSONL from disk (`server.py:1676-1702`). Minor simplification: use `_ensure_bus_activated` to uniformly resolve the session dir regardless of whether the run is active or historical. Existing behavior otherwise unchanged.

### 4.5 New endpoint: `POST /api/runs/{run_id}/rewrite`

Non-blocking endpoint. Returns 202 immediately. Replaces both `/review/rewrite` and the `action='rewrite'` case of `/gate/paper_qa`.

```python
@app.post("/api/runs/{run_id}/rewrite")
async def rewrite_paper(run_id: str, body: RewriteBody):
    run = _require_run(run_id)
    bus, pipeline, brief = _ensure_bus_activated(run)

    def _worker():
        asyncio.run(_run_rewrite_bg(run, bus, pipeline, brief,
                                     revision_prompt=body.revision_prompt))

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "rewriting"}, 202
```

`_run_rewrite_bg` (rewritten). The current implementation (inline in `POST /review/rewrite`, `server.py:2089-2208`) is ~120 lines. The new version reuses the existing `PaperQAHandler` construction pattern but removes all inline housekeeping because `_do_rewrite` now fires the writer-complete hook on success (§4.8):

```python
async def _run_rewrite_bg(run, bus, pipeline, brief, revision_prompt):
    orchestrator = MetaOrchestrator(bus=bus, client=create_client())
    handler = PaperQAHandler(
        bus=bus, agents=orchestrator.agents, router=orchestrator.router,
        client=orchestrator.client, tool_registry=orchestrator.tool_registry,
        skill_injector=orchestrator.skill_injector, memory=orchestrator.memory,
        gate_controller=orchestrator.gate,
    )

    session_dir = settings.runs_dir / run.run_id
    backup_dir = session_dir.parent / f"{run.run_id}.backup"

    # Filesystem-level backup is the safety net if the rewrite crashes
    # after _do_rewrite's in-memory restore runs. _do_rewrite restores
    # task.outputs; the backup restores paper.tex / paper.pdf on disk.
    if session_dir.is_dir():
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir)
        shutil.copytree(session_dir, backup_dir)

    try:
        new_latex = await handler._do_rewrite(
            pipeline, brief, revision_prompt=revision_prompt,
        )
        if new_latex:
            # _do_rewrite's §4.8 call site 2 already wrote paper.tex,
            # bumped paper_version, removed stale paper.pdf, persisted
            # bus. Append the rewrite marker here (single source of
            # truth for the JSONL, lives server-side).
            _append_paper_qa_rewrite_marker(run.run_id, revision_prompt)
            if backup_dir.is_dir():
                shutil.rmtree(backup_dir)
        else:
            _restore_from_backup(run, session_dir, backup_dir)
    except Exception:
        _restore_from_backup(run, session_dir, backup_dir)
        raise
```

`_append_paper_qa_rewrite_marker` and `_restore_from_backup` are promoted from nested `self.…` handler methods to module-level helpers in `server.py`. This deletes ~60 lines of duplicated housekeeping and leaves `_run_rewrite_bg` focused on its one responsibility: coordinate the rewrite and guarantee rollback on failure.

### 4.6 `_do_rewrite` includes experiment

`paper_qa_handler.py:327-447`. Current:
```python
rewrite_tasks = ["writer"] if writer_only else ["theory", "writer"]
```

New:
```python
rewrite_tasks = ["writer"] if writer_only else ["experiment", "theory", "writer"]
```

Experiment is reset alongside theory and writer. Snapshot/restore logic (lines 362-371, 431-443) extended to cover the experiment task by the same pattern already used for theory and writer. This matches the existing UI semantics where a rewrite means "redo the downstream research, not just the prose."

### 4.7 Writer-complete hook (new module)

`eurekaclaw/ui/writer_hook.py`:

```python
from pathlib import Path
from eurekaclaw.knowledge_bus.bus import KnowledgeBus

def on_writer_complete(bus: KnowledgeBus, session_id: str,
                        session_dir: Path) -> None:
    """Post-writer artifact housekeeping. Called from:
      - MetaOrchestrator main loop after writer task completes
      - PaperQAHandler._do_rewrite on rewrite success
    Reads writer latex from the pipeline (same pattern as
    _sync_latex_to_disk). No-op when writer output is missing/empty.
    """
    pipeline = bus.get_pipeline()
    if not pipeline:
        return
    writer = next((t for t in pipeline.tasks if t.name == "writer"), None)
    if writer is None or not writer.outputs:
        return
    latex = writer.outputs.get("latex_paper", "") or ""
    if not latex:
        return
    _bump_writer_paper_version(bus)       # moved from server.py:1264-1284
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "paper.tex").write_text(latex, encoding="utf-8")
    stale_pdf = session_dir / "paper.pdf"
    if stale_pdf.exists():
        stale_pdf.unlink()                # invalidate cache — next compile rebuilds
    bus.persist(session_dir)


def _bump_writer_paper_version(bus: KnowledgeBus) -> int:
    """Moved verbatim from server.py:1264-1284."""
    pipeline = bus.get_pipeline()
    if not pipeline:
        return 0
    writer = next((t for t in pipeline.tasks if t.name == "writer"), None)
    if writer is None:
        return 0
    outputs = writer.outputs or {}
    new_version = int(outputs.get("paper_version", 1)) + 1
    outputs["paper_version"] = new_version
    writer.outputs = outputs
    bus.put_pipeline(pipeline)
    return new_version
```

Because `_bump_writer_paper_version` is moved (not duplicated), `server.py` imports it back from `eurekaclaw.ui.writer_hook` at the one remaining call site (inside `/compile-pdf` if any — actually none after this refactor; check with grep during step 1 and remove the old definition).

### 4.8 Pipeline integration of the hook

The hook has two call sites because initial writer runs go through `MetaOrchestrator.run()` while rewrites go through `PaperQAHandler._do_rewrite()` — `_do_rewrite` invokes `agent.execute(task)` directly (`paper_qa_handler.py:402`) rather than re-entering the orchestrator's main loop.

**Call site 1** — `meta_orchestrator.py` main loop, after line 262 (`task.mark_completed(task_outputs)`) and before line 263 (`[green]✓ Done[/green]`). Fires for initial writer runs:

```python
if task.name == "writer":
    from eurekaclaw.ui.writer_hook import on_writer_complete
    on_writer_complete(
        self.bus,
        brief.session_id,
        settings.runs_dir / brief.session_id,
    )
```

**Call site 2** — `paper_qa_handler.py::_do_rewrite`, immediately after the successful-rewrite `self.bus.put_pipeline(pipeline)` at line 446 and before the `return self._get_latex_from_pipeline(pipeline)` at line 447. Only runs on the success path (after the `if rewrite_failed:` branch returns), so a failed rewrite that restores previous outputs does not re-fire the hook:

```python
self.bus.put_pipeline(pipeline)
from eurekaclaw.ui.writer_hook import on_writer_complete
on_writer_complete(
    self.bus,
    brief.session_id,
    settings.runs_dir / brief.session_id,
)
return self._get_latex_from_pipeline(pipeline)
```

This second call site also benefits CLI mode: when a CLI user rewrites from stdin, the same housekeeping runs. `_run_rewrite_bg` in `server.py` therefore contains zero housekeeping code — the hook is the single enforcer.

### 4.9 Deleted pipeline stage

`eurekaclaw/orchestrator/pipelines/default_pipeline.yaml:75-81` — delete the entire `paper_qa_gate` stage:

```yaml
# DELETED:
  - name: paper_qa_gate
    agent_role: orchestrator
    description: "Review the generated paper — optionally ask a question or request a rewrite"
    inputs: {}
    depends_on: [writer]
    gate_required: false
    max_retries: 0
```

Writer becomes the terminal stage of the default pipeline.

### 4.10 Deleted orchestrator code

- `meta_orchestrator.py:217-221` — the `paper_qa_gate` branch inside the orchestrator-task handling block. The generic `if task.agent_role == "orchestrator": task.mark_completed(); continue` remains for any future orchestrator stages.
- `meta_orchestrator.py:711+` — `_handle_paper_qa_gate` method entirely.
- `paper_qa_handler.py::_run_ui_mode` — the UI-mode entrypoint. CLI-mode `run()` and `_do_rewrite(...)` stay; only `_run_ui_mode` is removed.

### 4.11 Deleted server endpoints

In `server.py`:
- `POST /api/runs/{run_id}/review` (lines 2057-2087) — deleted. Bus activation is now implicit via `_ensure_bus_activated`.
- `POST /api/runs/{run_id}/review/rewrite` (lines 2089-2208) — deleted. Replaced by `POST /api/runs/{run_id}/rewrite` (§4.5).
- `/gate/paper_qa` case inside `POST /api/runs/{run_id}/gate/{type}` (lines 2334-2353) — deleted. No more gate endpoint for paper QA; Ask/Rewrite are first-class endpoints.
- `_append_paper_qa_rewrite_marker` (lines 2393-2412) — if still used by the remaining handlers, moved to module level; if all remaining callers have disappeared, deleted.

**CLI mode preserved.** `paper_qa_handler.run()` (called from `eurekaclaw run …` CLI, not from the UI server) is untouched. CLI users still get the full ask/rewrite/no interactive flow via stdin. The UI path is the only one being deleted.

---

## 5. Tests

### 5.1 Backend unit tests (tests/unit/)

Pattern: `tmp_path` + `_FakeRun` / `_FakeSession` dataclass fixtures, already established in `tests/unit/test_sync_latex_to_disk.py`.

- `test_writer_hook.py`
  - `on_writer_complete` writes `paper.tex` to `session_dir`.
  - Unlinks a pre-existing stale `paper.pdf`.
  - Bumps `paper_version` from 1 → 2 on a second writer completion.
  - Persists the bus (checks `bus.persist` was called, or a sentinel on-disk artifact exists).
  - Idempotent: calling twice with the same bus state leaves the same `paper.tex` and does not double-bump the version. (Version bump is tied to the bus value, not the hook call count — verify by asserting the second call is a no-op when `bus.get_writer_output()` is unchanged by the caller.)
  - No-op when `bus.get_writer_output()` returns empty string.

- `test_compile_pdf_self_heal.py`
  - `/compile-pdf` with `paper.tex` absent but `bus.get_writer_output()` returning latex: the handler writes `paper.tex` from the bus and then invokes pdflatex. (Mock pdflatex.)
  - `/compile-pdf` with both absent: returns 400.
  - `/compile-pdf` with `paper.tex` present: behavior unchanged from today.

- `test_rewrite_endpoint.py`
  - `POST /api/runs/{id}/rewrite` returns 202 immediately, even with a slow `_do_rewrite` (mock it to sleep).
  - Verifies a background thread was spawned (patched `threading.Thread`).
  - `_do_rewrite` (isolated test) with `writer_only=False` resets experiment + theory + writer task statuses back to `PENDING`. Snapshot/restore logic preserves original `outputs` for those three tasks when the rewrite fails.
  - `_do_rewrite` fires `on_writer_complete` exactly once on success (patch the hook, assert `call_count == 1`).
  - `_do_rewrite` does NOT fire `on_writer_complete` when the rewrite fails and restores previous outputs.
  - `_run_rewrite_bg` restores from filesystem backup when `_do_rewrite` returns `None`; deletes the backup on success.

- `test_ensure_bus_activated.py`
  - Active run (session.bus has writer output): returns the session's live bus/pipeline/brief — no disk read.
  - Historical run (session is None or bus is empty): reads `session_dir/bus.json` (or equivalent) and returns a hydrated bus.
  - Run dir missing: raises/returns appropriately (match existing `/paper-qa/history` behavior).

### 5.2 Frontend tests (extend `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`)

Pattern: `vi.mock('@/api/client')` + `renderHook` + `waitFor` + `act`. Existing file uses this exact pattern.

**Replace/update existing tests:**

- Tests that reference `mode === 'gate'`, `mode === 'loading-review'`, `mode === 'completed'`, `mode === 'failed'`: rewrite to use the new `'no-paper' | 'ready' | 'rewriting'` enum. E.g., `it('yields mode=ready when writer has output and no task is rewriting')`.
- Test `review-activation failure falls back to completed mode with reviewError set`: DELETE. No more `reviewStatus`, no more `reviewError`, no more `POST /review` call.
- Test `onAccept posts no-action to the gate endpoint in gate mode`: DELETE. No more Accept button.
- Test `onRewrite in completed mode POSTs to /review/rewrite`: rewrite to POST to `/rewrite` (the new endpoint).
- Test `onRewrite in gate mode POSTs to /gate/paper_qa with rewrite action`: DELETE. Gate mode is gone; both branches of `onRewrite` collapse into one POST to `/rewrite`.
- Test `history-load failure preserves optimistic rewrite markers`: retain; trigger path updated (Effect 2 now fires whenever `hasPaper`, not only in gate/rewriting mode).

**New tests:**

- `onAsk POSTs to /paper-qa/ask and appends optimistic user message`.
- `onRewrite POSTs to /rewrite, appends marker, does NOT flip mode to rewriting by itself` (the mode flip comes from the pipeline state, not the callback).
- `mode === 'rewriting' when theory or writer is in_progress/running/pending AFTER writer has produced output` (the paper exists from the prior pass, but a rewrite is underway).
- `isHistorical is not present on the session object` — type-level check or absence assertion, to prevent regression.

### 5.3 Manual verification (at user's keyboard)

Design target: once the paper renders, there is **one state** (`ready`). New sessions and historical sessions are indistinguishable. Ask and Rewrite are actions within that state.

1. **Unified `ready` state works identically from both entry paths:**
   - Open a historical completed session from the sessions list → PDF renders immediately, chat history loads from disk, Ask + Rewrite both work.
   - Start a new session → writer finishes → identical UX engages. No Accept click, no "Loading paper review…", no error flash, no "pdflatex produced no PDF" error. Ask + Rewrite behave the same as in the historical case.
2. **Rewrite round-trip:** type a prompt → click Rewrite → marker appears in chat immediately → pipeline flips to `rewriting` (viewer shows state) → new PDF renders when writer re-finishes → back to `ready`. Must work in both a fresh session and an already-persisted one.
3. **Session switching:** flip between two sessions (one mid-rewrite, one completed) → per-session state, no bleed-through, no stale chat from the other session.

---

## 6. Rollout Order

One commit per step. Steps 1–5 are additive (both old and new paths exist). Steps 6–7 switch the frontend to the new paths. Steps 8–9 delete the old paths. This ordering ensures no intermediate commit breaks the running app: the old UI keeps working through step 5, the new UI works starting at step 7, and the dead code comes out in 8–9 only when nothing references it.

1. `eurekaclaw/ui/writer_hook.py` module + `test_writer_hook.py`.
2. Wire call site 1 in `meta_orchestrator.py`. E2e smoke: new session → `paper.tex` present immediately after writer finishes.
3. `/compile-pdf` self-heal + `test_compile_pdf_self_heal.py`.
4. `_ensure_bus_activated` helper + `test_ensure_bus_activated.py`. Refactor `/paper-qa/ask` and `/paper-qa/history` to use it. (Old `/review*` endpoints still exist and still work.)
5. `POST /api/runs/{id}/rewrite` endpoint + rewritten `_run_rewrite_bg` + `test_rewrite_endpoint.py`. `_do_rewrite` extended with experiment. Call site 2 wired. (Old `/review/rewrite` still exists.)
6. Frontend: simplify `usePaperSession.ts` + update vitest (single commit). New sessions now use `/paper-qa/ask` and `/rewrite`; old `/review*` endpoints stop being called by the UI.
7. Frontend: simplify `PaperPanel.tsx`, rename and simplify `QAChat.tsx` → `PaperChat.tsx` (single commit). Accept button gone, `loading-review`/`failed`/`gate` branches gone.
8. Delete `/review`, `/review/rewrite`, `/gate/paper_qa` handlers in `server.py`. Safe because no frontend caller remains after step 6.
9. Delete `paper_qa_gate` stage (`default_pipeline.yaml`), `_handle_paper_qa_gate` (orchestrator), `_run_ui_mode` (paper_qa_handler). New sessions now end at writer and the UI no longer expects the gate.
10. Manual verification per §5.3. Fix any issues before merging.

---

## 7. Out of Scope

- Streaming assistant replies in chat. The current `/paper-qa/ask` returns a single JSON response; keep it that way for this change.
- Paper diff view between versions. `paper_version` is already bumped; a future change can add a "show previous version" toggle without touching this design.
- Multi-turn conversations with back-references. Each Ask is independent, same as today.
- Any change to CLI mode of `paper_qa_handler`. Only the UI-facing `_run_ui_mode` and its dependents are removed.
