# Paper Tab Unified Ready-State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse post-writer Paper-tab UI to a single `ready` state (new sessions and historical sessions indistinguishable), fix the PDF-render race on new sessions, and consolidate five overlapping backend endpoints into two.

**Architecture:** A pipeline-level writer-complete hook (`eurekaclaw/ui/writer_hook.py::on_writer_complete`) makes `paper.tex` an invariant of writer completion, fired from both the main orchestrator loop and the rewrite handler. A single `_ensure_bus_activated` helper replaces three bus-hydration copies in `server.py`. Frontend `usePaperSession` mode collapses from 6 values to 3 (`'no-paper' | 'ready' | 'rewriting'`), removing Accept and merging Ask + Rewrite into one chat.

**Tech Stack:**
- Python 3.11+, `pytest`, dataclasses for `_FakeRun`/`_FakeSession` fixtures (already established in `tests/unit/test_sync_latex_to_disk.py`)
- React 18, TypeScript, Vitest with `@testing-library/react`, `vi.mock('@/api/client')` (already used in `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`)
- `http.server` BaseHTTPRequestHandler pattern (existing `server.py`), threading.Thread + asyncio.run for non-blocking rewrite

**Spec:** `docs/superpowers/specs/2026-04-17-paper-tab-unified-ready-state-design.md` (commit `e3b3e7b`)

**Supersedes:** `docs/superpowers/plans/2026-04-17-paper-rewrite-unification.md` (draft, uncommitted; can be deleted locally once this plan is merged).

---

## File Structure

**New files:**
- `eurekaclaw/ui/writer_hook.py` — `on_writer_complete` + moved `_bump_writer_paper_version`
- `tests/unit/test_writer_hook.py`
- `tests/unit/test_compile_pdf_self_heal.py`
- `tests/unit/test_ensure_bus_activated.py`
- `tests/unit/test_rewrite_endpoint.py`
- `frontend/src/components/workspace/paper-review/PaperChat.tsx` (replaces `QAChat.tsx` via rename + edit)

**Modified files:**
- `eurekaclaw/orchestrator/meta_orchestrator.py` — add call site 1; delete `paper_qa_gate` branch + `_handle_paper_qa_gate`
- `eurekaclaw/orchestrator/paper_qa_handler.py` — extend `_do_rewrite` with experiment; add call site 2; delete `_run_ui_mode`
- `eurekaclaw/orchestrator/pipelines/default_pipeline.yaml` — delete `paper_qa_gate` stage
- `eurekaclaw/ui/server.py` — self-heal `/compile-pdf`; add `_ensure_bus_activated`; refactor `/paper-qa/ask`, `/paper-qa/history`; add `POST /rewrite`; delete `/review`, `/review/rewrite`, `/gate/paper_qa`; delete old `_bump_writer_paper_version` (moved)
- `frontend/src/components/workspace/paper-review/usePaperSession.ts`
- `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`
- `frontend/src/components/workspace/PaperPanel.tsx`
- `frontend/src/components/workspace/paper-review/QAChat.tsx` → renamed to `PaperChat.tsx`

**Deleted files:** None — all deletions are within existing files.

---

## Task 1: Writer-complete hook module

**Files:**
- Create: `eurekaclaw/ui/writer_hook.py`
- Create: `tests/unit/test_writer_hook.py`

This task is purely additive. The hook is not wired to any call site yet (that's Task 2 and Task 5); only the module + unit tests ship here.

- [ ] **Step 1.1: Write the failing tests**

Create `tests/unit/test_writer_hook.py` with the following content. The fixture pattern mirrors `tests/unit/test_sync_latex_to_disk.py` exactly.

```python
"""Tests for on_writer_complete — the writer-complete hook."""

from pathlib import Path

import pytest

from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus


def _make_bus_with_writer(session_id: str, latex: str,
                           paper_version: int | None = None) -> KnowledgeBus:
    bus = KnowledgeBus(session_id)
    outputs: dict = {"latex_paper": latex}
    if paper_version is not None:
        outputs["paper_version"] = paper_version
    writer = Task(
        task_id="w1", name="writer", agent_role="writer",
        description="Write paper", status=TaskStatus.COMPLETED,
        outputs=outputs,
    )
    bus.put_pipeline(TaskPipeline(
        pipeline_id="p1", session_id=session_id, tasks=[writer],
    ))
    return bus


def test_hook_writes_paper_tex(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = _make_bus_with_writer("sid-1", r"\section{Intro}")
    session_dir = tmp_path / "sid-1"
    on_writer_complete(bus, "sid-1", session_dir)

    tex = session_dir / "paper.tex"
    assert tex.is_file()
    assert tex.read_text(encoding="utf-8") == r"\section{Intro}"


def test_hook_unlinks_stale_paper_pdf(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = _make_bus_with_writer("sid-2", r"\section{v2}")
    session_dir = tmp_path / "sid-2"
    session_dir.mkdir()
    stale = session_dir / "paper.pdf"
    stale.write_bytes(b"%PDF-1.5\nstub")

    on_writer_complete(bus, "sid-2", session_dir)

    assert not stale.exists()


def test_hook_bumps_paper_version(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = _make_bus_with_writer("sid-3", r"\section{v1}", paper_version=1)
    session_dir = tmp_path / "sid-3"
    on_writer_complete(bus, "sid-3", session_dir)

    pipeline = bus.get_pipeline()
    writer = next(t for t in pipeline.tasks if t.name == "writer")
    assert writer.outputs["paper_version"] == 2


def test_hook_persists_bus(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = _make_bus_with_writer("sid-4", r"\section{Persisted}")
    session_dir = tmp_path / "sid-4"
    on_writer_complete(bus, "sid-4", session_dir)

    # KnowledgeBus.persist writes <key>.json for each stored key.
    assert (session_dir / "task_pipeline.json").is_file()


def test_hook_noop_on_empty_latex(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = _make_bus_with_writer("sid-5", "")
    session_dir = tmp_path / "sid-5"
    on_writer_complete(bus, "sid-5", session_dir)

    assert not (session_dir / "paper.tex").exists()


def test_hook_noop_when_no_writer_task(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = KnowledgeBus("sid-6")
    bus.put_pipeline(TaskPipeline(pipeline_id="p6", session_id="sid-6", tasks=[]))
    session_dir = tmp_path / "sid-6"
    on_writer_complete(bus, "sid-6", session_dir)

    assert not (session_dir / "paper.tex").exists()


def test_hook_noop_when_no_pipeline(tmp_path: Path):
    from eurekaclaw.ui.writer_hook import on_writer_complete

    bus = KnowledgeBus("sid-7")   # no put_pipeline call
    session_dir = tmp_path / "sid-7"
    on_writer_complete(bus, "sid-7", session_dir)

    assert not (session_dir / "paper.tex").exists()
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_writer_hook.py -v`
Expected: All 7 tests FAIL with `ModuleNotFoundError: No module named 'eurekaclaw.ui.writer_hook'`.

- [ ] **Step 1.3: Check whether the `task_pipeline.json` key assumption is correct**

Run: `python -m pytest tests/unit/test_sync_latex_to_disk.py -v` to confirm the bus persistence pattern works. Then `grep -n 'task_pipeline' eurekaclaw/knowledge_bus/bus.py` to confirm the exact key `KnowledgeBus` uses for the pipeline.

If the key is different (e.g., `pipeline.json` or `tasks.json`), adjust `test_hook_persists_bus` accordingly. Do NOT guess — verify against the bus implementation.

- [ ] **Step 1.4: Write the hook module**

Create `eurekaclaw/ui/writer_hook.py`:

```python
"""Writer-complete hook — post-writer artifact housekeeping.

Single source of truth for the invariant: once the writer agent produces
latex_paper, the session directory contains a fresh paper.tex, paper.pdf
is invalidated, paper_version is bumped, and the bus is persisted.

Call sites:
  - eurekaclaw/orchestrator/meta_orchestrator.py (initial writer run)
  - eurekaclaw/orchestrator/paper_qa_handler.py _do_rewrite (rewrite success)
"""

from __future__ import annotations

from pathlib import Path

from eurekaclaw.knowledge_bus.bus import KnowledgeBus


def on_writer_complete(
    bus: KnowledgeBus,
    session_id: str,
    session_dir: Path,
) -> None:
    """Run post-writer housekeeping.

    No-op if the bus has no pipeline, no writer task, or empty latex.
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
    _bump_writer_paper_version(bus)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "paper.tex").write_text(latex, encoding="utf-8")
    stale_pdf = session_dir / "paper.pdf"
    if stale_pdf.exists():
        stale_pdf.unlink()
    bus.persist(session_dir)


def _bump_writer_paper_version(bus: KnowledgeBus) -> int:
    """Increment writer.outputs['paper_version'] on the bus pipeline.

    Moved from eurekaclaw/ui/server.py. Treats a missing field as 1 (so
    first-time callers see 2 after the bump, matching existing
    frontend expectations for v1 → v2 on first rewrite). Returns the
    new version. No-op (returns 0) if no writer task is found.
    """
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

- [ ] **Step 1.5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_writer_hook.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 1.6: Run the sync_latex test to confirm no regressions**

Run: `python -m pytest tests/unit/test_sync_latex_to_disk.py -v`
Expected: All existing tests PASS — Task 1 did not touch `_sync_latex_to_disk`.

- [ ] **Step 1.7: Commit**

```bash
git add eurekaclaw/ui/writer_hook.py tests/unit/test_writer_hook.py
git commit -m "$(cat <<'EOF'
feat(writer_hook): add on_writer_complete + unit tests

Adds the post-writer housekeeping invariant as a standalone module:
writes paper.tex from the bus pipeline, unlinks stale paper.pdf,
bumps paper_version, persists bus. Not yet wired to any call site.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire call site 1 in MetaOrchestrator

**Files:**
- Modify: `eurekaclaw/orchestrator/meta_orchestrator.py:262-263`
- Modify: `tests/unit/test_writer_hook.py` (add integration test)

- [ ] **Step 2.1: Read the exact call-site context**

Run: `python -c "import eurekaclaw.orchestrator.meta_orchestrator as m; import inspect; print(inspect.getsourcefile(m))"` then open that file. Locate the block:

```python
else:
    task_outputs = dict(result.output)
    if result.text_summary:
        task_outputs["text_summary"] = result.text_summary
    if result.token_usage:
        task_outputs["token_usage"] = result.token_usage
    task.mark_completed(task_outputs)
    console.print(f"[green]✓ Done: {task.name}[/green]")
```

The insertion is between `task.mark_completed(task_outputs)` and the `console.print(...)` line.

- [ ] **Step 2.2: Write the failing integration test**

Add to `tests/unit/test_writer_hook.py` (append at end of file):

```python
def test_meta_orchestrator_fires_hook_after_writer(monkeypatch, tmp_path):
    """After a writer task completes in MetaOrchestrator, paper.tex exists."""
    from eurekaclaw.orchestrator import meta_orchestrator as mo
    from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus

    bus = _make_bus_with_writer("sid-mo", r"\section{Orch}")
    session_dir = tmp_path / "sid-mo"

    # Patch settings.runs_dir so the hook writes under tmp_path.
    monkeypatch.setattr(mo.settings, "runs_dir", tmp_path)

    # Simulate the exact call the orchestrator main-loop will make.
    from eurekaclaw.ui.writer_hook import on_writer_complete
    pipeline = bus.get_pipeline()
    writer = next(t for t in pipeline.tasks if t.name == "writer")
    assert writer.name == "writer"    # sanity
    on_writer_complete(bus, "sid-mo", tmp_path / "sid-mo")

    assert (session_dir / "paper.tex").read_text(encoding="utf-8") \
        == r"\section{Orch}"
```

This test is a specification check — it locks the call-site contract. The real orchestrator invocation happens in the manual-verification e2e (Task 10), not here.

- [ ] **Step 2.3: Run — should pass already**

Run: `python -m pytest tests/unit/test_writer_hook.py::test_meta_orchestrator_fires_hook_after_writer -v`
Expected: PASS (the test only asserts the hook behavior; the real wiring still needs to be added).

- [ ] **Step 2.4: Add the hook call in meta_orchestrator.py**

Edit `eurekaclaw/orchestrator/meta_orchestrator.py` around line 262. Insert AFTER `task.mark_completed(task_outputs)` and BEFORE `console.print(f"[green]✓ Done: {task.name}[/green]")`:

```python
                task.mark_completed(task_outputs)
                if task.name == "writer":
                    from eurekaclaw.ui.writer_hook import on_writer_complete
                    on_writer_complete(
                        self.bus,
                        brief.session_id,
                        settings.runs_dir / brief.session_id,
                    )
                console.print(f"[green]✓ Done: {task.name}[/green]")
```

(The import is inside the `if` block to avoid a module-load cycle between `orchestrator` and `ui`.)

- [ ] **Step 2.5: Run the full unit-test suite**

Run: `python -m pytest tests/unit/ -v`
Expected: All tests PASS, including the new writer_hook tests and the existing sync_latex tests.

- [ ] **Step 2.6: E2E smoke (manual, quick)**

Run: `python -m eurekaclaw --help` to confirm the orchestrator still imports cleanly after the edit.

If your environment has a `prove` or `research` command that can be dry-run (check `python -m eurekaclaw --help`), trigger one and confirm no `ImportError` or `AttributeError`. A full end-to-end new-session run is not required here — Task 10 covers that.

- [ ] **Step 2.7: Commit**

```bash
git add eurekaclaw/orchestrator/meta_orchestrator.py tests/unit/test_writer_hook.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): fire writer-complete hook after writer task

Wires on_writer_complete into the MetaOrchestrator main loop for
initial writer runs. Rewrite path gets wired in Task 5.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `/compile-pdf` self-heal

**Files:**
- Modify: `eurekaclaw/ui/server.py` (the `/compile-pdf` handler around line 1897)
- Create: `tests/unit/test_compile_pdf_self_heal.py`

- [ ] **Step 3.1: Read the current `/compile-pdf` handler**

Run: `grep -n 'compile-pdf' eurekaclaw/ui/server.py` and read the full handler. Note in particular the line that calls `_sync_latex_to_disk(run)` — the self-heal will augment that call.

- [ ] **Step 3.2: Write the failing tests**

Create `tests/unit/test_compile_pdf_self_heal.py`:

```python
"""Tests for the /compile-pdf self-heal behavior.

/compile-pdf must succeed when paper.tex is missing but the bus has a
writer output. Verifies paper.tex is written from the bus before the
pdflatex invocation.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus


@dataclass
class _FakeSession:
    bus: Any


@dataclass
class _FakeRun:
    output_dir: str | None
    eureka_session: Any
    eureka_session_id: str = "sid-compile"
    run_id: str = "run-compile"


def _make_run(tmp_path: Path, latex: str,
              write_tex: bool = False) -> _FakeRun:
    bus = KnowledgeBus("sid-compile")
    writer = Task(
        task_id="w1", name="writer", agent_role="writer",
        description="Write", status=TaskStatus.COMPLETED,
        outputs={"latex_paper": latex},
    )
    bus.put_pipeline(TaskPipeline(
        pipeline_id="p1", session_id="sid-compile", tasks=[writer],
    ))
    session = _FakeSession(bus=bus)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    if write_tex:
        (output_dir / "paper.tex").write_text(latex, encoding="utf-8")
    return _FakeRun(output_dir=str(output_dir), eureka_session=session)


def test_self_heal_writes_tex_from_bus_when_disk_empty(tmp_path: Path):
    from eurekaclaw.ui.server import _self_heal_paper_tex

    run = _make_run(tmp_path, r"\section{Heal}", write_tex=False)
    output_dir = Path(run.output_dir)
    assert not (output_dir / "paper.tex").exists()

    _self_heal_paper_tex(run)

    assert (output_dir / "paper.tex").read_text(encoding="utf-8") \
        == r"\section{Heal}"


def test_self_heal_noop_when_disk_present(tmp_path: Path):
    """Existing paper.tex is not overwritten when bus matches disk."""
    from eurekaclaw.ui.server import _self_heal_paper_tex

    run = _make_run(tmp_path, r"\section{Exists}", write_tex=True)
    output_dir = Path(run.output_dir)
    mtime_before = (output_dir / "paper.tex").stat().st_mtime_ns

    _self_heal_paper_tex(run)

    mtime_after = (output_dir / "paper.tex").stat().st_mtime_ns
    assert mtime_before == mtime_after


def test_self_heal_noop_when_bus_empty(tmp_path: Path):
    from eurekaclaw.ui.server import _self_heal_paper_tex

    run = _make_run(tmp_path, "", write_tex=False)
    output_dir = Path(run.output_dir)

    _self_heal_paper_tex(run)

    assert not (output_dir / "paper.tex").exists()
```

- [ ] **Step 3.3: Run tests — verify they fail**

Run: `python -m pytest tests/unit/test_compile_pdf_self_heal.py -v`
Expected: All FAIL with `ImportError: cannot import name '_self_heal_paper_tex'`.

- [ ] **Step 3.4: Add `_self_heal_paper_tex` helper in server.py**

Edit `eurekaclaw/ui/server.py`. Insert after the existing `_sync_latex_to_disk` function (around line 1317):

```python
def _self_heal_paper_tex(run) -> None:
    """Ensure paper.tex exists on disk before /compile-pdf runs.

    Thin wrapper around _sync_latex_to_disk: if paper.tex is missing
    but the bus has a writer output, write it. This is the belt to
    the writer-complete hook's suspenders — under normal flow the
    hook has already written the file, but a race or a historical
    session that never triggered the hook still compiles.
    """
    if not getattr(run, "output_dir", None):
        return
    tex_path = Path(run.output_dir) / "paper.tex"
    if tex_path.is_file():
        return
    _sync_latex_to_disk(run)
```

- [ ] **Step 3.5: Run tests — verify they pass**

Run: `python -m pytest tests/unit/test_compile_pdf_self_heal.py -v`
Expected: All 3 PASS.

- [ ] **Step 3.6: Wire the helper into `/compile-pdf`**

Edit `eurekaclaw/ui/server.py` — find the `/compile-pdf` handler. Locate the line that checks or requires `paper.tex` before invoking pdflatex. Insert a call to `_self_heal_paper_tex(run)` immediately before that check.

The exact insertion point: look for `_sync_latex_to_disk(run)` or `paper.tex` inside the `/compile-pdf` handler. Replace the existing `_sync_latex_to_disk(run)` call (if any) with `_self_heal_paper_tex(run)` — the self-heal supersedes the sync because it covers the "disk empty" case the sync already handles *plus* the case where the hook has already written paper.tex (in which case self-heal is a cheap no-op).

Note: if the current `/compile-pdf` returns an early error like `{"error": "no paper.tex", ...}` when the file is missing, remove that early return — `_self_heal_paper_tex` now provides the file, and the subsequent pdflatex step is the authoritative check.

- [ ] **Step 3.7: Re-run tests**

Run: `python -m pytest tests/unit/test_compile_pdf_self_heal.py tests/unit/test_sync_latex_to_disk.py -v`
Expected: All PASS.

- [ ] **Step 3.8: Commit**

```bash
git add eurekaclaw/ui/server.py tests/unit/test_compile_pdf_self_heal.py
git commit -m "$(cat <<'EOF'
fix(compile-pdf): self-heal paper.tex from bus when disk is empty

Fixes the PDF-render race on new sessions. /compile-pdf now writes
paper.tex from the bus pipeline if the file is missing, regardless
of whether the writer-complete hook has fired yet.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `_ensure_bus_activated` helper + refactor paper-qa endpoints

**Files:**
- Modify: `eurekaclaw/ui/server.py` (add helper; refactor `/paper-qa/ask` and `/paper-qa/history`)
- Create: `tests/unit/test_ensure_bus_activated.py`

- [ ] **Step 4.1: Write the failing tests**

Create `tests/unit/test_ensure_bus_activated.py`:

```python
"""Tests for _ensure_bus_activated.

Verifies:
  - Active run (session + bus with pipeline) → returns in-memory bus.
  - Historical run (no live session) → hydrates from disk via SessionLoader.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.types.artifacts import ResearchBrief
from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus


@dataclass
class _FakeSession:
    bus: Any


@dataclass
class _FakeRun:
    eureka_session: Any
    eureka_session_id: str = "sid-ensure"
    run_id: str = "run-ensure"


def _make_active_run() -> _FakeRun:
    bus = KnowledgeBus("sid-ensure")
    bus._store["research_brief"] = ResearchBrief(
        session_id="sid-ensure", input_mode="research", domain="d",
        query="q", conjecture=None, selected_skills=[], reference_paper_ids=[],
    )
    writer = Task(
        task_id="w1", name="writer", agent_role="writer",
        description="Write", status=TaskStatus.COMPLETED,
        outputs={"latex_paper": r"\section{Live}"},
    )
    bus.put_pipeline(TaskPipeline(
        pipeline_id="p1", session_id="sid-ensure", tasks=[writer],
    ))
    return _FakeRun(eureka_session=_FakeSession(bus=bus))


def test_active_run_returns_in_memory_bus():
    from eurekaclaw.ui.server import _ensure_bus_activated

    run = _make_active_run()
    bus, pipeline, brief = _ensure_bus_activated(run)

    assert bus is run.eureka_session.bus
    assert pipeline.tasks[0].outputs["latex_paper"] == r"\section{Live}"
    assert brief.session_id == "sid-ensure"


def test_historical_run_hydrates_via_session_loader(tmp_path: Path):
    from eurekaclaw.ui import server as srv

    run = _FakeRun(eureka_session=None)

    sentinel_bus = KnowledgeBus("sid-ensure")
    sentinel_pipeline = TaskPipeline(
        pipeline_id="pX", session_id="sid-ensure", tasks=[],
    )
    sentinel_brief = ResearchBrief(
        session_id="sid-ensure", input_mode="research", domain="d",
        query="q", conjecture=None, selected_skills=[], reference_paper_ids=[],
    )

    with patch(
        "eurekaclaw.orchestrator.session_loader.SessionLoader.load",
        return_value=(sentinel_bus, sentinel_brief, sentinel_pipeline),
    ) as mock_load:
        bus, pipeline, brief = srv._ensure_bus_activated(run)

    mock_load.assert_called_once_with("sid-ensure")
    assert bus is sentinel_bus
    assert pipeline is sentinel_pipeline
    assert brief is sentinel_brief


def test_session_without_pipeline_triggers_disk_load():
    """Session exists but bus never had put_pipeline — treat as historical."""
    from eurekaclaw.ui import server as srv

    empty_bus = KnowledgeBus("sid-ensure")        # no pipeline set
    run = _FakeRun(eureka_session=_FakeSession(bus=empty_bus))

    sentinel_bus = KnowledgeBus("sid-ensure")
    sentinel_pipeline = TaskPipeline(
        pipeline_id="pX", session_id="sid-ensure", tasks=[],
    )
    sentinel_brief = ResearchBrief(
        session_id="sid-ensure", input_mode="research", domain="d",
        query="q", conjecture=None, selected_skills=[], reference_paper_ids=[],
    )

    with patch(
        "eurekaclaw.orchestrator.session_loader.SessionLoader.load",
        return_value=(sentinel_bus, sentinel_brief, sentinel_pipeline),
    ):
        bus, _, _ = srv._ensure_bus_activated(run)

    assert bus is sentinel_bus
```

- [ ] **Step 4.2: Run tests — verify they fail**

Run: `python -m pytest tests/unit/test_ensure_bus_activated.py -v`
Expected: All FAIL with `ImportError: cannot import name '_ensure_bus_activated'`.

- [ ] **Step 4.3: Add the helper to server.py**

Edit `eurekaclaw/ui/server.py`. Insert near the top of the helpers block (before `_bump_writer_paper_version`, which will be deleted in Task 1's follow-up — wait, it was already moved in Task 1. Insert in a suitable helper region, e.g., right after `_sync_latex_to_disk`):

```python
def _ensure_bus_activated(run):
    """Return (bus, pipeline, brief) for a run.

    Uses the live EurekaSession's bus if it has a pipeline; otherwise
    hydrates from disk via SessionLoader.load(session_id). Raises
    FileNotFoundError / ValueError if the session_id is unknown and
    cannot be loaded — callers must handle that.
    """
    session = getattr(run, "eureka_session", None)
    if session is not None:
        bus = getattr(session, "bus", None)
        if bus is not None and bus.get_pipeline() is not None:
            return bus, bus.get_pipeline(), bus.get_research_brief()
    from eurekaclaw.orchestrator.session_loader import SessionLoader
    bus, brief, pipeline = SessionLoader.load(run.eureka_session_id)
    return bus, pipeline, brief
```

- [ ] **Step 4.4: Run tests — verify they pass**

Run: `python -m pytest tests/unit/test_ensure_bus_activated.py -v`
Expected: All 3 PASS.

- [ ] **Step 4.5: Refactor `/paper-qa/ask` to use the helper**

Edit `eurekaclaw/ui/server.py`. Find the `POST /api/runs/{run_id}/paper-qa/ask` handler (around line 2213). Locate the block that currently hydrates the bus inline (look for `run.eureka_session`, `SessionLoader.load`, or `session.bus.get_pipeline()`). Replace that block with:

```python
try:
    bus, pipeline, brief = _ensure_bus_activated(run)
except (FileNotFoundError, ValueError) as exc:
    self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
    return
```

Preserve the rest of the handler (request body parsing, QA agent invocation, response). The variable names `bus`, `pipeline`, `brief` must match what the rest of the handler already uses — if it uses different names, either rename them or map `_ensure_bus_activated`'s return through local variables.

- [ ] **Step 4.6: Refactor `/paper-qa/history` to use the helper**

Edit the same file. Find the `GET /api/runs/{run_id}/paper-qa/history` handler (around line 1676-1702). Currently the handler reads `run.eureka_session_id` and goes straight to `settings.runs_dir / session_id / "paper_qa_history.jsonl"`.

The session-id source is already disk-based, so the helper isn't strictly necessary for the history read itself. **However**, call `_ensure_bus_activated(run)` once at the top of the handler so that an active session's in-memory bus gets a chance to persist any pending JSONL before we read — this catches the edge case where the server received a paper-qa/ask POST but hasn't yet flushed to disk.

Insert at the top of the handler, after the `run is None` / `session_id` guards:

```python
try:
    bus, _, _ = _ensure_bus_activated(run)
    # Ensure the bus has persisted any pending history before we read.
    bus.persist(settings.runs_dir / session_id)
except (FileNotFoundError, ValueError):
    # No disk session yet — fall through to empty history.
    pass
```

- [ ] **Step 4.7: Smoke-test both endpoints**

Run the full unit-test suite:
`python -m pytest tests/unit/ -v`
Expected: All existing tests PASS; new ensure_bus_activated tests PASS.

If there are integration tests under `tests/integration/` that hit `/paper-qa/ask` or `/paper-qa/history`, run them too.

- [ ] **Step 4.8: Commit**

```bash
git add eurekaclaw/ui/server.py tests/unit/test_ensure_bus_activated.py
git commit -m "$(cat <<'EOF'
refactor(server): extract _ensure_bus_activated helper

Consolidates bus-hydration logic across /paper-qa/ask and
/paper-qa/history. Active sessions return the in-memory bus;
historical sessions load via SessionLoader.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `/rewrite` endpoint + `_do_rewrite` extension + call site 2

**Files:**
- Modify: `eurekaclaw/orchestrator/paper_qa_handler.py` (extend `_do_rewrite`, add call site 2)
- Modify: `eurekaclaw/ui/server.py` (add `POST /api/runs/{run_id}/rewrite`, rewrite `_run_rewrite_bg`, add `_append_paper_qa_rewrite_marker` module-level helper, add `_restore_from_backup` helper)
- Create: `tests/unit/test_rewrite_endpoint.py`

This is the biggest backend task. Split into sub-tasks 5a (handler changes), 5b (server endpoint), 5c (tests).

### 5a. Extend `_do_rewrite` with experiment + call site 2

- [ ] **Step 5a.1: Read the current `_do_rewrite`**

Read `eurekaclaw/orchestrator/paper_qa_handler.py:327-447`. Note three regions:
- Line 360: `rewrite_tasks` definition
- Lines 362-371: snapshot of prior outputs
- Lines 431-443: failure-path restore
- Line 446: `self.bus.put_pipeline(pipeline)` right before `return self._get_latex_from_pipeline(pipeline)`

- [ ] **Step 5a.2: Write the failing test for experiment inclusion**

Append to `tests/unit/test_rewrite_endpoint.py` (or create if not yet present). Use this minimal scaffolding:

```python
"""Tests for the rewrite endpoint and _do_rewrite extensions."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.types.artifacts import ResearchBrief
from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus


def _make_pipeline_with_three_tasks(session_id: str) -> TaskPipeline:
    return TaskPipeline(
        pipeline_id="p1", session_id=session_id,
        tasks=[
            Task(
                task_id="e1", name="experiment", agent_role="experiment",
                description="exp", status=TaskStatus.COMPLETED,
                outputs={"experiment_result": "r1"},
            ),
            Task(
                task_id="t1", name="theory", agent_role="theory",
                description="theory", status=TaskStatus.COMPLETED,
                outputs={"theory_state": "s1"},
            ),
            Task(
                task_id="w1", name="writer", agent_role="writer",
                description="write", status=TaskStatus.COMPLETED,
                outputs={"latex_paper": r"\section{v1}", "paper_version": 1},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_do_rewrite_resets_experiment_theory_writer(tmp_path: Path):
    """rewrite_tasks must include experiment when writer_only=False."""
    from eurekaclaw.orchestrator.paper_qa_handler import PaperQAHandler

    bus = KnowledgeBus("sid-dr")
    pipeline = _make_pipeline_with_three_tasks("sid-dr")
    bus.put_pipeline(pipeline)
    brief = ResearchBrief(
        session_id="sid-dr", input_mode="research", domain="d",
        query="q", conjecture=None, selected_skills=[], reference_paper_ids=[],
    )

    handler = PaperQAHandler.__new__(PaperQAHandler)    # bypass __init__
    handler.bus = bus
    handler.router = MagicMock()
    handler._save_rewrite_context = MagicMock()
    handler._summarize_qa_history = MagicMock(return_value="")
    handler._get_latex_from_pipeline = MagicMock(return_value=r"\section{v2}")

    # Agent execute returns a success result with new outputs.
    def _resolve(task):
        agent = MagicMock()
        new_output = {
            "experiment": {"experiment_result": "r2"},
            "theory": {"theory_state": "s2"},
            "writer": {"latex_paper": r"\section{v2}"},
        }[task.name]
        agent.execute = AsyncMock(return_value=MagicMock(
            failed=False, output=new_output, text_summary=None,
        ))
        return agent
    handler.router.resolve = _resolve

    with patch("eurekaclaw.orchestrator.paper_qa_handler.settings") as mock_settings:
        mock_settings.runs_dir = tmp_path
        with patch("eurekaclaw.ui.writer_hook.on_writer_complete") as mock_hook:
            result = await handler._do_rewrite(
                pipeline, brief, revision_prompt="x", writer_only=False,
            )

    assert result == r"\section{v2}"
    # Experiment was reset and re-ran:
    assert pipeline.tasks[0].outputs["experiment_result"] == "r2"
    assert pipeline.tasks[1].outputs["theory_state"] == "s2"
    assert pipeline.tasks[2].outputs["latex_paper"] == r"\section{v2}"
    # Call site 2 fired exactly once:
    mock_hook.assert_called_once()


@pytest.mark.asyncio
async def test_do_rewrite_does_not_fire_hook_on_failure(tmp_path: Path):
    from eurekaclaw.orchestrator.paper_qa_handler import PaperQAHandler

    bus = KnowledgeBus("sid-fail")
    pipeline = _make_pipeline_with_three_tasks("sid-fail")
    bus.put_pipeline(pipeline)
    brief = ResearchBrief(
        session_id="sid-fail", input_mode="research", domain="d",
        query="q", conjecture=None, selected_skills=[], reference_paper_ids=[],
    )

    handler = PaperQAHandler.__new__(PaperQAHandler)
    handler.bus = bus
    handler.router = MagicMock()
    handler._save_rewrite_context = MagicMock()
    handler._summarize_qa_history = MagicMock(return_value="")

    # Writer fails → whole rewrite fails (theory is allowed to fail, but
    # writer failure is unrecoverable in the current code path).
    def _resolve(task):
        agent = MagicMock()
        if task.name == "writer":
            agent.execute = AsyncMock(return_value=MagicMock(
                failed=True, error="boom", output={}, text_summary=None,
            ))
        else:
            agent.execute = AsyncMock(return_value=MagicMock(
                failed=False,
                output={"experiment_result": "r2"} if task.name == "experiment"
                       else {"theory_state": "s2"},
                text_summary=None,
            ))
        return agent
    handler.router.resolve = _resolve

    with patch("eurekaclaw.ui.writer_hook.on_writer_complete") as mock_hook:
        result = await handler._do_rewrite(
            pipeline, brief, revision_prompt="x", writer_only=False,
        )

    assert result is None
    mock_hook.assert_not_called()
```

Add `pytest-asyncio` marker support at the top of the file if not already there:
```python
pytestmark = pytest.mark.asyncio
```
(or install `pytest-asyncio` via existing `requirements-dev.txt` if the repo already uses it — verify with `grep asyncio requirements*.txt`).

- [ ] **Step 5a.3: Run the test — verify it fails**

Run: `python -m pytest tests/unit/test_rewrite_endpoint.py::test_do_rewrite_resets_experiment_theory_writer -v`
Expected: FAIL — either because `rewrite_tasks = ["theory", "writer"]` only touches theory and writer (so the experiment task's outputs remain `"r1"`), or because the hook was not called.

- [ ] **Step 5a.4: Extend `_do_rewrite`**

Edit `eurekaclaw/orchestrator/paper_qa_handler.py`:

**Change 1 (line 360):**
```python
# Before:
rewrite_tasks = ["writer"] if writer_only else ["theory", "writer"]
# After:
rewrite_tasks = ["writer"] if writer_only else ["experiment", "theory", "writer"]
```

**Change 2 (extend snapshot, lines 362-371):** Add experiment snapshot alongside theory/writer. Locate the existing block:

```python
theory_task = next((t for t in pipeline.tasks if t.name == "theory"), None)
writer_task = next((t for t in pipeline.tasks if t.name == "writer"), None)
prev_theory_outputs = dict(theory_task.outputs) if theory_task else {}
prev_writer_outputs = dict(writer_task.outputs) if writer_task else {}
prev_theory_desc = theory_task.description if theory_task else ""
```

Extend to:

```python
experiment_task = next((t for t in pipeline.tasks if t.name == "experiment"), None)
theory_task = next((t for t in pipeline.tasks if t.name == "theory"), None)
writer_task = next((t for t in pipeline.tasks if t.name == "writer"), None)
prev_experiment_outputs = dict(experiment_task.outputs) if experiment_task else {}
prev_theory_outputs = dict(theory_task.outputs) if theory_task else {}
prev_writer_outputs = dict(writer_task.outputs) if writer_task else {}
prev_theory_desc = theory_task.description if theory_task else ""
```

**Change 3 (extend reset, lines 378-385):** Add experiment reset alongside theory. Locate:

```python
if not writer_only and theory_task is not None:
    theory_task.description = (theory_task.description or "") + feedback
    theory_task.retries = 0
    theory_task.status = TaskStatus.PENDING
if writer_task is not None:
    writer_task.retries = 0
    writer_task.status = TaskStatus.PENDING
```

Extend to:

```python
if not writer_only and experiment_task is not None:
    experiment_task.retries = 0
    experiment_task.status = TaskStatus.PENDING
if not writer_only and theory_task is not None:
    theory_task.description = (theory_task.description or "") + feedback
    theory_task.retries = 0
    theory_task.status = TaskStatus.PENDING
if writer_task is not None:
    writer_task.retries = 0
    writer_task.status = TaskStatus.PENDING
```

**Change 4 (extend failure-path restore, lines 431-443):** Add experiment restore. Locate:

```python
if rewrite_failed:
    if theory_task is not None:
        theory_task.status = TaskStatus.COMPLETED
        theory_task.outputs = prev_theory_outputs
        theory_task.error_message = ""
        theory_task.description = prev_theory_desc
    if writer_task is not None:
        writer_task.status = TaskStatus.COMPLETED
        writer_task.outputs = prev_writer_outputs
        writer_task.error_message = ""
    self.bus.put_pipeline(pipeline)
    return None
```

Extend to:

```python
if rewrite_failed:
    if experiment_task is not None:
        experiment_task.status = TaskStatus.COMPLETED
        experiment_task.outputs = prev_experiment_outputs
        experiment_task.error_message = ""
    if theory_task is not None:
        theory_task.status = TaskStatus.COMPLETED
        theory_task.outputs = prev_theory_outputs
        theory_task.error_message = ""
        theory_task.description = prev_theory_desc
    if writer_task is not None:
        writer_task.status = TaskStatus.COMPLETED
        writer_task.outputs = prev_writer_outputs
        writer_task.error_message = ""
    self.bus.put_pipeline(pipeline)
    return None
```

**Change 5 (call site 2):** After line 446 `self.bus.put_pipeline(pipeline)` and before line 447 `return self._get_latex_from_pipeline(pipeline)`, insert:

```python
from eurekaclaw.ui.writer_hook import on_writer_complete
from eurekaclaw.config import settings
on_writer_complete(
    self.bus,
    brief.session_id,
    settings.runs_dir / brief.session_id,
)
```

(Imports are local to keep the module import-light; `settings` may already be imported at module top — if so, drop the local import.)

- [ ] **Step 5a.5: Run the tests**

Run: `python -m pytest tests/unit/test_rewrite_endpoint.py -v`
Expected: Both `test_do_rewrite_resets_experiment_theory_writer` and `test_do_rewrite_does_not_fire_hook_on_failure` PASS.

- [ ] **Step 5a.6: Commit**

```bash
git add eurekaclaw/orchestrator/paper_qa_handler.py tests/unit/test_rewrite_endpoint.py
git commit -m "$(cat <<'EOF'
feat(paper_qa): include experiment in rewrite + fire hook on success

_do_rewrite now resets experiment + theory + writer (was theory +
writer) and fires on_writer_complete on the success path only.
Failed rewrites restore all three task outputs from the snapshot.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### 5b. Add `POST /api/runs/{id}/rewrite` endpoint

- [ ] **Step 5b.1: Write the failing endpoint test**

Append to `tests/unit/test_rewrite_endpoint.py`:

```python
def test_rewrite_endpoint_returns_202_and_spawns_thread():
    """POST /rewrite must return 202 immediately and spawn a worker thread."""
    from eurekaclaw.ui import server as srv

    # Minimal fake run + handler setup — we test _spawn_rewrite directly
    # rather than spinning up the full HTTP server.
    calls = []

    def fake_thread_ctor(target, daemon):
        calls.append((target, daemon))
        t = MagicMock()
        t.start = MagicMock()
        return t

    run = MagicMock()
    run.run_id = "run-rw"

    with patch.object(srv, "threading") as mock_threading:
        mock_threading.Thread = fake_thread_ctor
        srv._spawn_rewrite(run, bus=MagicMock(), pipeline=MagicMock(),
                            brief=MagicMock(), revision_prompt="fix X")

    assert len(calls) == 1
    target, daemon = calls[0]
    assert daemon is True
    assert callable(target)


def test_run_rewrite_bg_restores_backup_on_none(tmp_path: Path):
    """When _do_rewrite returns None, filesystem backup is restored."""
    from eurekaclaw.ui import server as srv

    session_dir = tmp_path / "sid-bg"
    backup_dir = tmp_path / "sid-bg.backup"
    session_dir.mkdir()
    (session_dir / "paper.tex").write_text("new-failed", encoding="utf-8")
    backup_dir.mkdir()
    (backup_dir / "paper.tex").write_text("original", encoding="utf-8")

    run = MagicMock()
    run.run_id = "sid-bg"
    run.eureka_session = None

    srv._restore_from_backup(run, session_dir, backup_dir)

    assert (session_dir / "paper.tex").read_text(encoding="utf-8") == "original"
    assert not backup_dir.exists()


def test_run_rewrite_bg_deletes_backup_on_success(tmp_path: Path):
    """After a successful rewrite, the backup is cleaned up."""
    from eurekaclaw.ui import server as srv

    backup_dir = tmp_path / "sid-ok.backup"
    backup_dir.mkdir()
    (backup_dir / "paper.tex").write_text("original", encoding="utf-8")

    srv._cleanup_backup(backup_dir)

    assert not backup_dir.exists()
```

- [ ] **Step 5b.2: Run — verify failures**

Run: `python -m pytest tests/unit/test_rewrite_endpoint.py -v`
Expected: The three new tests FAIL with missing `_spawn_rewrite`, `_restore_from_backup`, `_cleanup_backup`.

- [ ] **Step 5b.3: Add helpers and endpoint to server.py**

Edit `eurekaclaw/ui/server.py`. Add module-level helpers near the other rewrite-related helpers (after `_sync_latex_to_disk`):

```python
def _append_paper_qa_rewrite_marker(session_id: str, revision_prompt: str) -> None:
    """Append a system-role rewrite marker to the session's history JSONL.

    Moved from the bound method inside the review-rewrite handler so
    _run_rewrite_bg can call it without needing `self`.
    """
    import json as _json
    from datetime import datetime, timezone
    history_path = settings.runs_dir / session_id / "paper_qa_history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "role": "system",
        "content": f'↻ Rewrite requested: "{revision_prompt}"',
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with history_path.open("a", encoding="utf-8") as fp:
        fp.write(_json.dumps(marker) + "\n")


def _restore_from_backup(run, session_dir: Path, backup_dir: Path) -> None:
    """Replace session_dir with backup_dir contents. Used after a failed rewrite."""
    import shutil as _shutil
    if not backup_dir.is_dir():
        return
    if session_dir.is_dir():
        _shutil.rmtree(session_dir)
    backup_dir.rename(session_dir)
    # Reload bus from restored backup if the run has a live session.
    if getattr(run, "eureka_session", None):
        try:
            from eurekaclaw.orchestrator.session_loader import SessionLoader
            restored_bus, _, _ = SessionLoader.load(run.eureka_session_id)
            run.eureka_session.bus = restored_bus
        except Exception:
            pass


def _cleanup_backup(backup_dir: Path) -> None:
    import shutil as _shutil
    if backup_dir.is_dir():
        _shutil.rmtree(backup_dir)


async def _run_rewrite_bg(run, bus, pipeline, brief, revision_prompt: str) -> None:
    """Execute the rewrite in a background task.

    _do_rewrite fires the writer-complete hook on success (call site 2),
    so this function contains no inline housekeeping — just filesystem
    backup/restore around the rewrite.
    """
    from eurekaclaw.orchestrator.meta_orchestrator import MetaOrchestrator
    from eurekaclaw.orchestrator.paper_qa_handler import PaperQAHandler
    import shutil as _shutil

    orchestrator = MetaOrchestrator(bus=bus, client=create_client())
    handler = PaperQAHandler(
        bus=bus, agents=orchestrator.agents, router=orchestrator.router,
        client=orchestrator.client, tool_registry=orchestrator.tool_registry,
        skill_injector=orchestrator.skill_injector, memory=orchestrator.memory,
        gate_controller=orchestrator.gate,
    )

    session_dir = settings.runs_dir / run.run_id
    backup_dir = session_dir.parent / f"{run.run_id}.backup"

    if session_dir.is_dir():
        if backup_dir.is_dir():
            _shutil.rmtree(backup_dir)
        _shutil.copytree(session_dir, backup_dir)

    try:
        new_latex = await handler._do_rewrite(
            pipeline, brief, revision_prompt=revision_prompt, writer_only=False,
        )
        if new_latex:
            _append_paper_qa_rewrite_marker(run.run_id, revision_prompt)
            _cleanup_backup(backup_dir)
        else:
            _restore_from_backup(run, session_dir, backup_dir)
    except Exception:
        _restore_from_backup(run, session_dir, backup_dir)
        raise


def _spawn_rewrite(run, bus, pipeline, brief, revision_prompt: str) -> None:
    """Fire _run_rewrite_bg in a daemon thread with its own asyncio loop."""
    import asyncio as _asyncio
    import threading

    def _worker():
        _asyncio.run(_run_rewrite_bg(run, bus, pipeline, brief, revision_prompt))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
```

Then add the endpoint route in the POST handler. Find the POST dispatch block in `server.py` (around line 2060 where `/review` and `/review/rewrite` are dispatched). Add a new branch BEFORE the existing `/review/rewrite` branch:

```python
# POST /api/runs/<run_id>/rewrite
parts_rw = parsed.path.strip("/").split("/")
if (len(parts_rw) == 4 and parts_rw[0] == "api" and parts_rw[1] == "runs"
        and parts_rw[3] == "rewrite"):
    run_id = parts_rw[2]
    run = self.state.get_run(run_id)
    if run is None:
        self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
        return
    try:
        bus, pipeline, brief = _ensure_bus_activated(run)
    except (FileNotFoundError, ValueError) as exc:
        self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    payload = self._read_json()
    revision_prompt = str(payload.get("revision_prompt", "")).strip()
    if not revision_prompt:
        self._send_json({"error": "No revision_prompt provided"},
                        status=HTTPStatus.BAD_REQUEST)
        return
    _spawn_rewrite(run, bus, pipeline, brief, revision_prompt)
    self._send_json({"status": "rewriting"}, status=HTTPStatus.ACCEPTED)
    return
```

- [ ] **Step 5b.4: Run tests — verify pass**

Run: `python -m pytest tests/unit/test_rewrite_endpoint.py -v`
Expected: All rewrite tests PASS.

- [ ] **Step 5b.5: Also run Task-4 tests to ensure no regression**

Run: `python -m pytest tests/unit/ -v`
Expected: All PASS.

- [ ] **Step 5b.6: Commit**

```bash
git add eurekaclaw/ui/server.py tests/unit/test_rewrite_endpoint.py
git commit -m "$(cat <<'EOF'
feat(server): add POST /rewrite endpoint with background execution

Non-blocking rewrite endpoint returns 202 immediately. _run_rewrite_bg
owns filesystem backup/restore; _do_rewrite owns the writer-complete
hook. _append_paper_qa_rewrite_marker promoted to module-level helper.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend overhaul (usePaperSession + PaperPanel + PaperChat)

**Files:**
- Modify: `frontend/src/components/workspace/paper-review/usePaperSession.ts`
- Modify: `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`
- Rename: `frontend/src/components/workspace/paper-review/QAChat.tsx` → `PaperChat.tsx`
- Modify: `frontend/src/components/workspace/paper-review/PaperChat.tsx` (after rename)
- Modify: `frontend/src/components/workspace/PaperPanel.tsx`
- Modify: any other site importing `QAChat` (verify with grep)

**Why one task instead of two:** the three files are interlocked by TypeScript. `usePaperSession` removes `onAccept` / `isHistorical` / `reviewError`; `PaperPanel` references those fields and passes them to `QAChat`; `QAChat` declares them as required props. A split commit (e.g., hook first, panel second) would fail to type-check in between. This is a single logical change.

- [ ] **Step 6.1: Write the updated test file (red)**

Replace the contents of `frontend/src/components/workspace/paper-review/usePaperSession.test.ts` with:

```typescript
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

  it('yields mode=ready when writer has output and no rewrite is in flight', async () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.mode).toBe('ready');
    expect(result.current?.hasPaper).toBe(true);
    await waitFor(() =>
      expect(apiGetMock).toHaveBeenCalledWith('/api/runs/run-1/paper-qa/history'),
    );
  });

  it('yields mode=rewriting when theory task is in_progress after writer had output', () => {
    const run = makeRun({
      pipeline: [
        writerTask(2),
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

  it('onAsk POSTs to /paper-qa/ask and optimistically appends user message', async () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
    const { result } = renderHook(() => usePaperSession(run));

    await waitFor(() => expect(result.current?.mode).toBe('ready'));

    await act(async () => {
      await result.current!.onAsk('what is Section 3?');
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/paper-qa/ask',
      { question: 'what is Section 3?' },
    );
    const userMsg = result.current!.messages.find(
      (m) => m.role === 'user' && m.content === 'what is Section 3?',
    );
    expect(userMsg).toBeDefined();
  });

  it('onRewrite POSTs to /rewrite and appends optimistic marker', async () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
    const { result } = renderHook(() => usePaperSession(run));

    await waitFor(() => expect(result.current?.mode).toBe('ready'));

    await act(async () => {
      await result.current!.onRewrite('fix Section 3');
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/rewrite',
      { revision_prompt: 'fix Section 3' },
    );
    const marker = result.current!.messages.find(
      (m) => m.role === 'system' && m.content.includes('fix Section 3'),
    );
    expect(marker).toBeDefined();
    expect(marker!.content).toBe('↻ Rewrite requested: "fix Section 3"');
  });

  it('onRewrite does not flip mode to rewriting by itself — pipeline drives that', async () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
    const { result } = renderHook(() => usePaperSession(run));
    await waitFor(() => expect(result.current?.mode).toBe('ready'));

    await act(async () => {
      await result.current!.onRewrite('tighten proofs');
    });

    expect(result.current?.mode).toBe('ready');
  });

  it('paperVersion reads writer.outputs.paper_version when present', () => {
    const run = makeRun({ pipeline: [writerTask(3)] });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.paperVersion).toBe(3);
  });

  it('paperVersion falls back to 1 + rewrite-marker count', async () => {
    const run = makeRun({
      pipeline: [
        {
          task_id: 'w1', name: 'writer', agent_role: 'writer',
          status: 'completed',
          outputs: { latex_paper: '\\section{X}' },
        } as PipelineTask,
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

  it('history-load failure preserves optimistic rewrite markers', async () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
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

  it('session object has no isHistorical / onAccept / reviewError fields', () => {
    const run = makeRun({ pipeline: [writerTask(1)] });
    const { result } = renderHook(() => usePaperSession(run));
    const session = result.current!;
    expect('isHistorical' in session).toBe(false);
    expect('onAccept' in session).toBe(false);
    expect('reviewError' in session).toBe(false);
  });
});
```

- [ ] **Step 6.2: Run vitest — verify failures**

Run: `cd frontend && npx vitest run src/components/workspace/paper-review/usePaperSession.test.ts`
Expected: Multiple FAILs — the current hook still has `mode === 'gate'`, `reviewStatus`, etc. Many tests will error out.

- [ ] **Step 6.3: Rewrite `usePaperSession.ts`**

Replace the contents of `frontend/src/components/workspace/paper-review/usePaperSession.ts` with:

```typescript
import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost } from '@/api/client';
import { REWRITE_MARKER_PREFIX } from '@/constants/paper';
import type { SessionRun, QAMessage } from '@/types';

export type PaperMode = 'no-paper' | 'ready' | 'rewriting';

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

  onAsk: (question: string) => Promise<void>;
  onRewrite: (prompt: string) => Promise<void>;
}

type HistoryResponse = { messages: QAMessage[] };

export function usePaperSession(run: SessionRun | null): PaperSession | null {
  const [messages, setMessages] = useState<QAMessage[]>([]);
  const [isRewriting, setIsRewriting] = useState(false);

  const writerTask = run?.pipeline?.find((t) => t.name === 'writer');
  const theoryTask = run?.pipeline?.find((t) => t.name === 'theory');
  const experimentTask = run?.pipeline?.find((t) => t.name === 'experiment');

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
    experimentTask?.status === 'in_progress' ||
    experimentTask?.status === 'running' ||
    experimentTask?.status === 'pending' ||
    theoryTask?.status === 'in_progress' ||
    theoryTask?.status === 'running' ||
    theoryTask?.status === 'pending' ||
    writerTask?.status === 'in_progress' ||
    writerTask?.status === 'running' ||
    writerTask?.status === 'pending';

  const mode: PaperMode = useMemo(() => {
    if (!hasPaper) return 'no-paper';
    if (pipelineRewriting) return 'rewriting';
    return 'ready';
  }, [hasPaper, pipelineRewriting]);

  // Load chat history whenever a paper exists. Triggers once on mount
  // and re-fires on run_id / hasPaper changes. Preserves optimistic
  // markers when the server history doesn't yet reflect them.
  useEffect(() => {
    if (!run?.run_id || !hasPaper) return;
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
              (m.role === 'system' &&
                typeof m.content === 'string' &&
                m.content.startsWith(REWRITE_MARKER_PREFIX) &&
                !serverKeys.has(`${m.role}|${m.content}`)) ||
              (m.role === 'user' &&
                typeof m.content === 'string' &&
                !serverKeys.has(`${m.role}|${m.content}`)),
          );
          return [...serverMsgs, ...optimistic];
        });
      } catch {
        setMessages((prev) =>
          prev.filter(
            (m) =>
              (m.role === 'system' &&
                typeof m.content === 'string' &&
                m.content.startsWith(REWRITE_MARKER_PREFIX)) ||
              m.role === 'user',
          ),
        );
      }
    })();
  }, [run?.run_id, hasPaper]);

  const onAsk = useCallback(
    async (question: string) => {
      if (!run?.run_id) return;
      const trimmed = question.trim();
      if (!trimmed) return;
      const userMsg: QAMessage = {
        role: 'user',
        content: trimmed,
        ts: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg]);
      try {
        await apiPost(`/api/runs/${run.run_id}/paper-qa/ask`, {
          question: trimmed,
        });
      } catch (e) {
        const errMsg: QAMessage = {
          role: 'system',
          content: `Question error: ${e instanceof Error ? e.message : String(e)}`,
          ts: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, errMsg]);
      }
    },
    [run?.run_id],
  );

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
          content: `Rewrite error: ${e instanceof Error ? e.message : String(e)}`,
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
    onAsk,
    onRewrite,
  };
}
```

- [ ] **Step 6.4: Inventory current `QAChat` imports**

Run:
```bash
grep -rn "QAChat" frontend/src/
```
Record every hit. These import sites need updating in Step 6.8.

- [ ] **Step 6.5: Rename `QAChat.tsx` → `PaperChat.tsx`**

```bash
git mv frontend/src/components/workspace/paper-review/QAChat.tsx \
       frontend/src/components/workspace/paper-review/PaperChat.tsx
```

- [ ] **Step 6.6: Rewrite `PaperChat.tsx`**

Read the old `QAChat.tsx` content (before staging the rewrite) if you need to reference current styling. Then replace the contents of `frontend/src/components/workspace/paper-review/PaperChat.tsx` with:

```tsx
import { useState } from 'react';
import type { QAMessage } from '@/types';

interface PaperChatProps {
  runId: string;
  messages: QAMessage[];
  setMessages: React.Dispatch<React.SetStateAction<QAMessage[]>>;
  disabled: boolean;
  onAsk: (question: string) => Promise<void>;
  onRewrite: (prompt: string) => Promise<void>;
}

export function PaperChat({
  messages,
  disabled,
  onAsk,
  onRewrite,
}: PaperChatProps) {
  const [text, setText] = useState('');

  const handleSend = async (kind: 'ask' | 'rewrite') => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setText('');
    if (kind === 'ask') {
      await onAsk(trimmed);
    } else {
      await onRewrite(trimmed);
    }
  };

  return (
    <div className="paper-chat">
      <div className="paper-chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`paper-chat-msg paper-chat-msg-${m.role}`}>
            {m.content}
          </div>
        ))}
      </div>
      <div className="paper-chat-input">
        <textarea
          className="paper-chat-textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask a question or describe a rewrite..."
          disabled={disabled}
          rows={3}
        />
        <div className="paper-chat-actions">
          <button
            type="button"
            className="paper-chat-btn paper-chat-btn-ask"
            disabled={disabled || !text.trim()}
            onClick={() => handleSend('ask')}
          >
            Ask
          </button>
          <button
            type="button"
            className="paper-chat-btn paper-chat-btn-rewrite"
            disabled={disabled || !text.trim()}
            onClick={() => handleSend('rewrite')}
          >
            Rewrite
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 6.7: Update `PaperPanel.tsx`**

Replace the contents of `frontend/src/components/workspace/PaperPanel.tsx` with:

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionRun } from '@/types';
import { PaperViewer } from './paper-review/PaperViewer';
import { PaperChat } from './paper-review/PaperChat';
import { usePaperSession } from './paper-review/usePaperSession';

interface PaperPanelProps {
  run: SessionRun | null;
}

const SPLIT_KEY = 'eurekaclaw-review-split';
const MIN_SPLIT = 30;
const MAX_SPLIT = 70;
const DEFAULT_SPLIT = 55;

function loadInitialSplit(): number {
  try {
    const saved = localStorage.getItem(SPLIT_KEY);
    if (!saved) return DEFAULT_SPLIT;
    const parsed = parseFloat(saved);
    if (!Number.isFinite(parsed)) return DEFAULT_SPLIT;
    return Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, parsed));
  } catch {
    return DEFAULT_SPLIT;
  }
}

export function PaperPanel({ run }: PaperPanelProps) {
  return <PaperPanelInner key={run?.run_id ?? '__none__'} run={run} />;
}

function PaperPanelInner({ run }: PaperPanelProps) {
  const session = usePaperSession(run);

  const [splitPct, setSplitPct] = useState(loadInitialSplit);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(SPLIT_KEY, String(splitPct));
    } catch {
      /* ignore quota errors */
    }
  }, [splitPct]);

  const handleMouseDown = useCallback(() => setIsDragging(true), []);

  useEffect(() => {
    if (!isDragging) return;
    function onMouseMove(e: MouseEvent) {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      setSplitPct(Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, pct)));
    }
    function onMouseUp() {
      setIsDragging(false);
    }
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, [isDragging]);

  if (!run) {
    return (
      <div className="paper-preview">
        <div className="paper-empty-state">
          <p>Launch a session to produce a research paper.</p>
        </div>
      </div>
    );
  }

  if (!session) {
    return null;
  }

  if (session.mode === 'no-paper') {
    const message =
      run.status === 'running'
        ? 'Paper will appear once the writer agent completes.'
        : run.error || 'No paper generated yet.';
    return (
      <div className="paper-preview">
        <div className="paper-empty-state">
          {run.status === 'running' ? (
            <div className="paper-progress-dots">
              <span /><span /><span />
            </div>
          ) : null}
          <p>{message}</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="paper-review-panel"
      ref={containerRef}
      style={{ userSelect: isDragging ? 'none' : undefined }}
    >
      <div style={{ flex: `0 0 ${splitPct}%`, minWidth: 0, display: 'flex' }}>
        <PaperViewer
          run={session.run}
          paperVersion={session.paperVersion}
          isRewriting={session.isRewriting}
          theoryStatus={session.theoryStatus}
          writerStatus={session.writerStatus}
        />
      </div>

      <div
        className={`review-divider${isDragging ? ' is-dragging' : ''}`}
        onMouseDown={handleMouseDown}
      >
        <div className="review-divider-handle" />
      </div>

      <div style={{ flex: 1, minWidth: 0, display: 'flex' }}>
        <PaperChat
          runId={session.run.run_id}
          messages={session.messages}
          setMessages={session.setMessages}
          disabled={session.isRewriting}
          onAsk={session.onAsk}
          onRewrite={session.onRewrite}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 6.8: Update all remaining `QAChat` imports**

Using the grep output from Step 6.4, edit each import to point at `PaperChat`. Typical change:

```tsx
// Before:
import { QAChat } from './paper-review/QAChat';
// After:
import { PaperChat } from './paper-review/PaperChat';
```

And any JSX usage `<QAChat ... />` → `<PaperChat ... />`, updating props to the new shape.

- [ ] **Step 6.9: Rewrite `usePaperSession.ts` and type-check**

Now that `PaperPanel` has been simplified to consume the new session shape (Step 6.7) and `PaperChat` has been renamed + simplified (Steps 6.5–6.6), do the hook rewrite from Step 6.3 — but re-run the type check here to confirm everything compiles together.

Run:
```bash
cd frontend && npx tsc --noEmit
```
Expected: No type errors. If there are, the likely causes are a missed `QAChat` import site (revisit Step 6.8) or a prop-name mismatch between `PaperPanel`'s usage and `PaperChatProps` (revisit Step 6.6).

- [ ] **Step 6.10: Run all frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All tests PASS.

- [ ] **Step 6.11: Build the frontend**

Run: `cd frontend && npm run build`
Expected: Clean build. The build emits under `eurekaclaw/ui/static/assets/`; `git status` after the build should show old asset hashes replaced by new ones.

- [ ] **Step 6.12: Commit**

```bash
git add frontend/src/components/workspace/paper-review/usePaperSession.ts \
        frontend/src/components/workspace/paper-review/usePaperSession.test.ts \
        frontend/src/components/workspace/paper-review/PaperChat.tsx \
        frontend/src/components/workspace/PaperPanel.tsx \
        eurekaclaw/ui/static/
git commit -m "$(cat <<'EOF'
refactor(frontend): unify paper-tab to no-paper/ready/rewriting

Collapses usePaperSession mode enum to 3 values, renames
QAChat→PaperChat with merged Ask+Rewrite input, simplifies
PaperPanel to one layout path. Deletes reviewStatus, reviewError,
isHistorical, onAccept; adds onAsk + onRewrite pointing at
/paper-qa/ask and /rewrite. Historical and new sessions are now
indistinguishable in the UI.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Delete `/review`, `/review/rewrite`, `/gate/paper_qa`

**Files:**
- Modify: `eurekaclaw/ui/server.py` (three handler deletions; possible deletion of `_bump_writer_paper_version` if nothing else calls it)

- [ ] **Step 7.1: Confirm no frontend calls remain**

Run:
```bash
grep -rn "/review\|/gate/paper_qa" frontend/src/
```
Expected output: no matches. If any match appears, fix the frontend caller FIRST — otherwise the deletion will break it.

- [ ] **Step 7.2: Delete `POST /review` handler**

Edit `eurekaclaw/ui/server.py`. Find the block starting around line 2057 (the `POST /api/runs/<run_id>/review` handler). Delete the entire `if` branch, including the path-match and the handler body. Verify the preceding and following `if` branches still syntactically enclose correctly.

- [ ] **Step 7.3: Delete `POST /review/rewrite` handler**

Find the block around line 2089 (the `POST /api/runs/<run_id>/review/rewrite` handler). Delete the entire branch.

- [ ] **Step 7.4: Delete `/gate/paper_qa` case**

Find the `/gate/<type>` dispatch around line 2334. Locate the `elif gate_type == "paper_qa":` case (around line 2334-2353). Delete that case. Keep the other gate-type cases intact.

- [ ] **Step 7.5: Delete the old `_bump_writer_paper_version` in server.py**

Search:
```bash
grep -n "_bump_writer_paper_version" eurekaclaw/ui/server.py
```
The function definition at ~line 1264-1284 was moved to `writer_hook.py` in Task 1 but probably still has its old definition here. Delete the OLD definition in server.py (since the moved version in writer_hook.py is authoritative).

Any remaining callers in server.py (if any — there should be none after Tasks 5 and 8) must be updated to:
```python
from eurekaclaw.ui.writer_hook import _bump_writer_paper_version
```
or better, call `on_writer_complete` directly if that's the full intent.

- [ ] **Step 7.6: Delete any bound-method `_append_paper_qa_rewrite_marker` that was on the POST handler class**

Its replacement is the module-level helper added in Task 5b.3. Search:
```bash
grep -n "_append_paper_qa_rewrite_marker\|def _append_paper_qa" eurekaclaw/ui/server.py
```
If an old `self._append_paper_qa_rewrite_marker` method still exists on the request handler class (around line 2393-2412), delete it. The module-level helper replaces it.

- [ ] **Step 7.7: Smoke test**

Run the full unit-test suite:
```bash
python -m pytest tests/unit/ -v
```
Run the frontend tests:
```bash
cd frontend && npx vitest run
```
Start the dev server (if the repo has a dev-server script), open a session, verify Paper tab loads. Look for any 404 responses in devtools network tab — they indicate a missed frontend caller.

- [ ] **Step 7.8: Commit**

```bash
git add eurekaclaw/ui/server.py
git commit -m "$(cat <<'EOF'
chore(server): delete /review, /review/rewrite, /gate/paper_qa handlers

Superseded by /paper-qa/ask, /paper-qa/history, and /rewrite. No
frontend callers remain. _bump_writer_paper_version moved to
eurekaclaw/ui/writer_hook.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Delete `paper_qa_gate` stage, `_handle_paper_qa_gate`, `_run_ui_mode`

**Files:**
- Modify: `eurekaclaw/orchestrator/pipelines/default_pipeline.yaml`
- Modify: `eurekaclaw/orchestrator/meta_orchestrator.py`
- Modify: `eurekaclaw/orchestrator/paper_qa_handler.py`

- [ ] **Step 8.1: Delete the yaml stage**

Edit `eurekaclaw/orchestrator/pipelines/default_pipeline.yaml`. Delete lines 75-81 (the entire `paper_qa_gate` stage):

```yaml
# DELETE THESE LINES:
  - name: paper_qa_gate
    agent_role: orchestrator
    description: "Review the generated paper — optionally ask a question or request a rewrite"
    inputs: {}
    depends_on: [writer]
    gate_required: false
    max_retries: 0
```

Also verify no other yaml pipeline references `paper_qa_gate`:
```bash
grep -rn "paper_qa_gate" eurekaclaw/orchestrator/pipelines/
```
Expected: no matches.

- [ ] **Step 8.2: Delete the orchestrator branch**

Edit `eurekaclaw/orchestrator/meta_orchestrator.py`. Find lines 217-221:

```python
# Execute orchestrator tasks (no agent needed)
if task.agent_role == "orchestrator":
    if task.name == "paper_qa_gate":
        await self._handle_paper_qa_gate(pipeline, brief)
    task.mark_completed()
    continue
```

Simplify to (remove only the inner `if task.name == "paper_qa_gate":` branch, keeping the generic orchestrator-task handling):

```python
# Execute orchestrator tasks (no agent needed)
if task.agent_role == "orchestrator":
    task.mark_completed()
    continue
```

- [ ] **Step 8.3: Delete `_handle_paper_qa_gate` method**

In the same file, find the `_handle_paper_qa_gate` method (around line 711). Delete the full method definition.

Also search for any other callers:
```bash
grep -n "_handle_paper_qa_gate" eurekaclaw/
```
Expected: no remaining matches after the deletion.

- [ ] **Step 8.4: Delete `_run_ui_mode` in paper_qa_handler.py**

Edit `eurekaclaw/orchestrator/paper_qa_handler.py`. Find the `_run_ui_mode` method. Delete it.

Search for any caller:
```bash
grep -n "_run_ui_mode" eurekaclaw/
```
Expected: no remaining matches.

If `run()` (the CLI entrypoint) referenced `_run_ui_mode`, it should already branch on an explicit mode flag; verify `run()` still works for CLI by reading it end-to-end.

- [ ] **Step 8.5: Run all tests**

```bash
python -m pytest tests/unit/ -v
cd frontend && npx vitest run
```
Expected: All PASS.

- [ ] **Step 8.6: Confirm CLI `prove` / `research` still works (quick smoke)**

Run `python -m eurekaclaw --help` (or whatever the project's CLI entry is). Confirm no import errors. If the repo has a cheap CLI dry-run command, execute it.

- [ ] **Step 8.7: Commit**

```bash
git add eurekaclaw/orchestrator/pipelines/default_pipeline.yaml \
        eurekaclaw/orchestrator/meta_orchestrator.py \
        eurekaclaw/orchestrator/paper_qa_handler.py
git commit -m "$(cat <<'EOF'
chore(pipeline): delete paper_qa_gate stage and UI-mode handler

Writer is now the terminal stage of the default pipeline. The UI
path is handled entirely by server endpoints; the CLI path retains
its interactive mode via PaperQAHandler.run(). Removes the last
reference to "gate" in the Paper-tab flow.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Manual verification

**Files:** None (verification only).

- [ ] **Step 9.1: Start the dev server**

Run the repo's dev command (check `package.json` or `Makefile` for the exact command — typical options: `make dev`, `npm run dev`, `python -m eurekaclaw.ui.server`).

- [ ] **Step 9.2: New session end-to-end**

In the UI, start a new session with any valid prompt. Observe:
- While the pipeline runs, the Paper tab shows the "Paper will appear once the writer agent completes" empty state (no errors).
- The moment the writer agent finishes, the Paper tab shows the PDF with no "Error: pdflatex ran but produced no PDF" error and no "Loading paper review..." screen.
- The chat panel shows an empty message list with the Ask + Rewrite input bar.
- There is NO Accept button anywhere.

- [ ] **Step 10.3: Ask a question**

Type a question in the chat, click Ask. Expected:
- The user message appears immediately (optimistic).
- The assistant reply appears within a few seconds.
- The PDF does not change.

- [ ] **Step 10.4: Request a rewrite**

Type a rewrite prompt, click Rewrite. Expected:
- An `↻ Rewrite requested: "..."` marker appears immediately in the chat.
- The viewer enters the rewriting state (progress indicator).
- After the pipeline re-runs experiment + theory + writer, the new PDF renders. Chat returns to normal.
- A new `paper_version` is reflected in the viewer header.

- [ ] **Step 10.5: Historical session**

Click an older completed session in the sessions sidebar. Expected:
- The Paper tab shows the stored PDF immediately.
- The chat history loads.
- Ask and Rewrite still work, identical to the new-session case. The Rewrite round-trip is especially important — confirm it enters `rewriting` and returns to `ready` with a new PDF.

- [ ] **Step 10.6: Session switching during rewrite**

Start a rewrite in session A. While it's running, click session B (another completed session). Expected:
- Session B's PDF and chat appear immediately.
- Session A's rewriting state does not bleed into session B's UI.
- Clicking back to session A shows its ongoing rewriting state.

- [ ] **Step 10.7: Network inspection**

Open devtools Network tab. During the above flows:
- Confirm NO requests to `/review`, `/review/rewrite`, or `/gate/paper_qa` — these endpoints no longer exist.
- Confirm Ask hits `/paper-qa/ask` and Rewrite hits `/rewrite`.
- Confirm the history endpoint `/paper-qa/history` is called on session load.

- [ ] **Step 10.8: Fix any issues discovered**

For each issue found in steps 10.2–10.7:
- Identify which task introduced the regression.
- Write a failing test that reproduces the issue in the appropriate test file.
- Fix the code.
- Run the test.
- Commit with a `fix:` prefix.

- [ ] **Step 10.9: Create the PR**

Once steps 10.2–10.7 pass cleanly:

```bash
git push -u origin shiyuan/paper-qa-gate
gh pr create --title "Paper tab: unified ready-state + PDF-render fix" --body "$(cat <<'EOF'
## Summary
- Collapses post-writer Paper-tab UI to a single `ready` state (removes Accept, unifies Ask + Rewrite into one chat).
- Fixes PDF-render race on new sessions via a pipeline-level writer-complete hook + `/compile-pdf` self-heal.
- Consolidates five overlapping backend endpoints into two (`/paper-qa/ask`, `/rewrite`) sharing a single `_ensure_bus_activated` helper.
- Deletes the `paper_qa_gate` pipeline stage and the UI-mode branch of `PaperQAHandler`; CLI mode preserved.

Spec: `docs/superpowers/specs/2026-04-17-paper-tab-unified-ready-state-design.md`
Plan: `docs/superpowers/plans/2026-04-17-paper-tab-unified-ready-state.md`

## Test plan
- [x] `python -m pytest tests/unit/` green
- [x] `cd frontend && npx vitest run` green
- [x] `cd frontend && npx tsc --noEmit` clean
- [x] Manual: new session → PDF renders without Accept click
- [x] Manual: Ask + Rewrite both work in new and historical sessions
- [x] Manual: Session switching during rewrite is clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

This plan was self-reviewed against the spec after being drafted. The following items are deliberately structured this way and should not be "fixed" by an executor:

1. **Task 2 is light on new test content.** The writer-hook behavior is already covered by Task 1's tests; Task 2's added test is a contract lock-in. The real orchestrator wiring is verified in Task 10's e2e.
2. **Task 5 is split into 5a + 5b** because it touches two files with independent concerns (handler extension vs. server endpoint). Keeping them in one commit would mix a refactor with a new endpoint.
3. **Task 7's `git mv` + rewrite pattern** may produce a large diff because git sees it as delete-and-add rather than a rename. That's expected; the intent is documented in the commit message.
4. **Task 8 depends on Task 7** (frontend stops calling old endpoints first). Executing Task 8 before Task 7 would temporarily 404 on every Accept click in the old UI. Do not reorder.
5. **Task 9 depends on Task 7** for the same reason (frontend must stop expecting `mode === 'gate'` before the yaml stage is removed).
6. **CLI mode of PaperQAHandler is preserved.** If `_run_ui_mode` turns out to still have a CLI caller when Task 9 runs, stop and investigate — the spec assumes only the UI server calls `_run_ui_mode`.
