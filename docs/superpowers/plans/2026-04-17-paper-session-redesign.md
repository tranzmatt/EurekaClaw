# Paper Session Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two Paper panel components (`PaperPanel`, `PaperReviewPanel`) into a single entry point backed by one `usePaperSession` state-machine hook, plus three small backend cleanups that the hook depends on.

**Architecture:**
- **Backend (surgical):** add a monotonically-increasing `paper_version` int to writer task outputs (so the frontend reads it directly instead of deriving from chat markers); extract the duplicated bus-to-disk LaTeX sync into `_sync_latex_to_disk(run)`; hoist the `"↻ Rewrite requested: "` literal into one shared `REWRITE_MARKER_PREFIX` constant on each side.
- **Frontend:** all state-machine logic moves into `usePaperSession(run)` — review activation, history load, optimistic rewrite markers, mode derivation, gate-vs-historical dispatch. `PaperPanel` becomes a pure render switch on `session.mode`. `PaperReviewPanel.tsx` and the `WorkspaceTabs` panel-takeover fork are deleted. Single draggable divider, `key={run.run_id}` remount, tab bar stays visible during gate/rewrite.

**Tech Stack:** Python 3.11 + pytest + pytest-asyncio (backend); React 18 + TypeScript + Vite 5.4 + Zustand (frontend); **vitest + @testing-library/react + jsdom** are added in Task 3 (not currently in the repo).

**Spec:** `docs/superpowers/specs/2026-04-17-paper-session-redesign-design.md` (commit `680009e`).

---

## File Structure

**Files created:**
- `eurekaclaw/ui/constants.py` — module-level `REWRITE_MARKER_PREFIX`.
- `frontend/src/constants/paper.ts` — matching TS constant.
- `frontend/src/components/workspace/paper-review/usePaperSession.ts` — the state-machine hook.
- `frontend/src/components/workspace/paper-review/usePaperSession.test.ts` — vitest suite.
- `frontend/src/test-setup.ts` — vitest global setup (jsdom matchers).
- `tests/unit/test_paper_version.py` — writer sets `1`, rewrites bump, failed rewrite does not bump.
- `tests/unit/test_sync_latex_to_disk.py` — noop when equal, writes when different, never unlinks paper.pdf.

**Files modified:**
- `eurekaclaw/agents/writer/agent.py` — `_make_result` output dict gains `"paper_version": 1`.
- `eurekaclaw/orchestrator/paper_qa_handler.py` — UI-mode rewrite-success branch bumps `writer.outputs["paper_version"]`; uses shared constant for the `↻ Rewrite requested: …` marker.
- `eurekaclaw/ui/server.py` — `/review/rewrite` success branch bumps `paper_version` via `_bump_writer_paper_version(bus)`; `/compile-pdf` + `/artifacts/paper.tex` delegate to `_sync_latex_to_disk(run)`; `_append_paper_qa_rewrite_marker` uses the shared constant.
- `tests/unit/test_paper_qa_handler.py` — extend with `_do_rewrite` bump coverage.
- `frontend/package.json` — adds `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`; `scripts.test`.
- `frontend/vite.config.ts` — adds `test` block with jsdom + setup file.
- `frontend/src/components/workspace/PaperPanel.tsx` — rewritten to consume `usePaperSession` and render by mode; single draggable divider.
- `frontend/src/components/workspace/WorkspaceTabs.tsx` — deletes the gate/rewrite takeover fork; `PaperPanel` always mounted.
- `frontend/src/styles/paper-review.css` — removes the now-unused `.review-divider-static` block.

**Files deleted:**
- `frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx` — subsumed by `PaperPanel` + `usePaperSession`.

---

## Task 1 — Backend: `paper_version` field

**Goal:** `writer.outputs["paper_version"]` is the single source of truth for the frontend paper version counter: `1` on first writer run, `+1` each time a rewrite (UI gate path or `/review/rewrite`) succeeds, unchanged on failure.

**Files:**
- Modify: `eurekaclaw/agents/writer/agent.py:506-512` (`_make_result` call).
- Modify: `eurekaclaw/orchestrator/paper_qa_handler.py:178-200` (gate-path rewrite-success branch).
- Modify: `eurekaclaw/ui/server.py:2125-2143` (`/review/rewrite` success branch).
- Modify: `tests/unit/test_paper_qa_handler.py` (existing suite).
- Create: `tests/unit/test_paper_version.py`.

---

### Task 1.1 — Writer agent sets `paper_version = 1`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_paper_version.py`:

```python
"""Tests for the `paper_version` field on writer task outputs."""

from unittest.mock import MagicMock

import pytest

from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus


def _writer_task() -> Task:
    return Task(
        task_id="w1",
        name="writer",
        agent_role="writer",
        description="Write paper",
    )


def test_writer_output_includes_paper_version_one():
    """A fresh writer run stamps paper_version=1 on its outputs."""
    from eurekaclaw.agents.writer.agent import WriterAgent

    # Build the minimal result dict the way the agent does on the success path.
    task = _writer_task()
    agent_output = {
        "latex_paper": r"\section{Intro} body",
        "word_count": 2,
        "output_format": "latex",
        "paper_version": 1,
    }
    result = WriterAgent._make_result(
        MagicMock(),  # `self` substitute — _make_result doesn't use self state
        task,
        success=True,
        output=agent_output,
        text_summary="ok",
        token_usage={"input": 0, "output": 0},
    )
    assert result.output["paper_version"] == 1
```

This test is currently aspirational — it documents what we want `WriterAgent._make_result` callers to produce. Next step is making it real by editing the actual call site in `agent.py`.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
.venv/bin/python -m pytest tests/unit/test_paper_version.py::test_writer_output_includes_paper_version_one -v
```

Expected: PASS (the test is self-contained — it manually adds `paper_version` to the output dict). This is a guard-rail test: it fails later if a refactor renames the field.

Now write the *real* regression test that exercises the agent's own output construction:

Append to `tests/unit/test_paper_version.py`:

```python
def test_writer_output_key_naming_uses_paper_version(monkeypatch):
    """Regression: writer.outputs must contain a literal 'paper_version' key
    (not 'version' or 'paperVersion') so the frontend hook can read it."""
    # Grep the source for the literal field name.
    import pathlib, re
    src = pathlib.Path("eurekaclaw/agents/writer/agent.py").read_text(encoding="utf-8")
    # _make_result call in the success path must include "paper_version".
    # Accept either single- or double-quoted.
    assert re.search(r'["\']paper_version["\']\s*:\s*1', src), (
        "writer agent success path must stamp paper_version=1 on outputs"
    )
```

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_paper_version.py::test_writer_output_key_naming_uses_paper_version -v
```

Expected: **FAIL** with `AssertionError: writer agent success path must stamp paper_version=1 on outputs` (the literal doesn't exist in `agent.py` yet).

- [ ] **Step 3: Write minimal implementation**

Edit `eurekaclaw/agents/writer/agent.py` at line 506-512 (the `_make_result` call):

```python
            return self._make_result(
                task,
                success=True,
                output={
                    output_key: paper_content,
                    "word_count": len(text.split()),
                    "output_format": fmt,
                    "paper_version": 1,
                },
                text_summary=f"Paper generated ({fmt}): {len(text.split())} words",
                token_usage=tokens,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest tests/unit/test_paper_version.py -v
```

Expected: both tests PASS.

Also run the existing test suite to confirm nothing regressed:

```bash
.venv/bin/python -m pytest tests/unit/test_paper_qa_handler.py -v
```

Expected: all existing tests PASS.

---

### Task 1.2 — `PaperQAHandler` bumps `paper_version` after gate-path rewrite

**Context:** In `paper_qa_handler.py:178-200`, after `_do_rewrite` returns a non-None `new_latex`, the handler currently writes a rewrite marker and calls `self._save_paper_version(new_latex)`. It also needs to mutate `pipeline.tasks[writer].outputs["paper_version"]` so when the frontend polls `/api/runs/<id>` next, the writer task reflects the new version.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_paper_qa_handler.py`:

```python
@pytest.mark.asyncio
async def test_gate_rewrite_success_bumps_paper_version(handler_setup, monkeypatch):
    """After _do_rewrite returns non-None, writer.outputs.paper_version
    increments by 1 (or starts at 1 if the writer didn't stamp it)."""
    handler, pipeline, brief = handler_setup

    # Seed: writer already has paper_version=1 from a prior successful run.
    writer_task = next(t for t in pipeline.tasks if t.name == "writer")
    writer_task.outputs["paper_version"] = 1

    # Stub _do_rewrite to return a fresh latex string (simulating success).
    monkeypatch.setattr(
        handler,
        "_do_rewrite",
        AsyncMock(return_value=r"\section{Intro v2}"),
    )

    # Stand in for the review_gate flow: invoke the rewrite branch directly.
    import os
    monkeypatch.setenv("EUREKACLAW_UI_MODE", "1")
    from eurekaclaw.ui import review_gate

    decisions = iter([
        type("D", (), {"action": "rewrite", "question": "fix Section 3"})(),
        type("D", (), {"action": "no", "question": ""})(),
    ])
    monkeypatch.setattr(review_gate, "wait_paper_qa", lambda _sid: next(decisions))
    monkeypatch.setattr(review_gate, "reset_paper_qa", lambda _sid: None)

    await handler.run(pipeline, brief)

    assert writer_task.outputs["paper_version"] == 2


@pytest.mark.asyncio
async def test_gate_rewrite_failure_does_not_bump_paper_version(
    handler_setup, monkeypatch
):
    """If _do_rewrite returns None (failure), paper_version stays put."""
    handler, pipeline, brief = handler_setup

    writer_task = next(t for t in pipeline.tasks if t.name == "writer")
    writer_task.outputs["paper_version"] = 3

    monkeypatch.setattr(handler, "_do_rewrite", AsyncMock(return_value=None))

    monkeypatch.setenv("EUREKACLAW_UI_MODE", "1")
    from eurekaclaw.ui import review_gate

    decisions = iter([
        type("D", (), {"action": "rewrite", "question": "fix X"})(),
        type("D", (), {"action": "no", "question": ""})(),
    ])
    monkeypatch.setattr(review_gate, "wait_paper_qa", lambda _sid: next(decisions))
    monkeypatch.setattr(review_gate, "reset_paper_qa", lambda _sid: None)

    await handler.run(pipeline, brief)

    assert writer_task.outputs["paper_version"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/unit/test_paper_qa_handler.py::test_gate_rewrite_success_bumps_paper_version -v
```

Expected: **FAIL** with `AssertionError: assert 1 == 2` (the handler doesn't mutate writer outputs yet).

- [ ] **Step 3: Write minimal implementation**

Edit `eurekaclaw/orchestrator/paper_qa_handler.py` around line 196-200 (inside `if new_latex is not None:`):

```python
                if new_latex is not None:
                    # Persist a rewrite marker matching the frontend's
                    # "↻" convention. Written AFTER success so a failed
                    # rewrite doesn't leave a marker in history for work
                    # that was never done.
                    marker = {
                        "role": "system",
                        "content": f'↻ Rewrite requested: "{decision.question}"',
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "version": self._paper_version,
                    }
                    self._history.append(marker)
                    self._session_dir.mkdir(parents=True, exist_ok=True)
                    marker_path = self._session_dir / "paper_qa_history.jsonl"
                    with marker_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(marker, ensure_ascii=False) + "\n")
                    self._save_paper_version(new_latex)
                    # Bump writer.outputs.paper_version so the polling
                    # frontend sees the new version without having to
                    # derive it from chat markers.
                    writer_task = next(
                        (t for t in pipeline.tasks if t.name == "writer"), None
                    )
                    if writer_task is not None:
                        outputs = writer_task.outputs or {}
                        outputs["paper_version"] = int(outputs.get("paper_version", 1)) + 1
                        writer_task.outputs = outputs
                        self.bus.put_pipeline(pipeline)
                    latex = new_latex
                    self.bus.put("paper_qa_latex", latex)
                else:
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/unit/test_paper_qa_handler.py -v
```

Expected: both new tests PASS along with the existing 5.

---

### Task 1.3 — `/review/rewrite` bumps `paper_version` on success

**Context:** `eurekaclaw/ui/server.py:2116-2143` calls `handler._do_rewrite(...)` and, on a truthy `new_latex`, writes `paper.tex` to disk and persists a marker. We also need to bump `writer.outputs["paper_version"]` exactly the way Task 1.2 does — via the bus pipeline.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_paper_version.py`:

```python
@pytest.mark.asyncio
async def test_review_rewrite_endpoint_bumps_paper_version(tmp_path, monkeypatch):
    """POST /review/rewrite → success → writer.outputs.paper_version bumps."""
    from eurekaclaw.knowledge_bus.bus import KnowledgeBus
    from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus
    from eurekaclaw.types.artifacts import ResearchBrief

    session_id = "test-rev-001"
    bus = KnowledgeBus(session_id)
    writer_task = Task(
        task_id="w1",
        name="writer",
        agent_role="writer",
        description="Write paper",
        status=TaskStatus.COMPLETED,
        outputs={"latex_paper": r"\section{v1}", "paper_version": 1},
    )
    pipeline = TaskPipeline(
        pipeline_id="p1", session_id=session_id, tasks=[writer_task]
    )
    bus.put_pipeline(pipeline)
    bus.put_research_brief(ResearchBrief(
        session_id=session_id,
        input_mode="exploration",
        domain="t",
        query="q",
    ))

    # Simulate what the handler does after a successful _do_rewrite:
    from eurekaclaw.ui.server import _bump_writer_paper_version

    new_version = _bump_writer_paper_version(bus)

    assert new_version == 2
    refreshed = bus.get_pipeline()
    wt = next(t for t in refreshed.tasks if t.name == "writer")
    assert wt.outputs["paper_version"] == 2


@pytest.mark.asyncio
async def test_bump_writer_paper_version_missing_field_defaults_to_one(tmp_path):
    """If the writer task predates this change (no paper_version), treat
    it as 1 and bump to 2."""
    from eurekaclaw.knowledge_bus.bus import KnowledgeBus
    from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus
    from eurekaclaw.ui.server import _bump_writer_paper_version

    bus = KnowledgeBus("test-rev-002")
    writer_task = Task(
        task_id="w1",
        name="writer",
        agent_role="writer",
        description="Write paper",
        status=TaskStatus.COMPLETED,
        outputs={"latex_paper": "body"},  # no paper_version key
    )
    bus.put_pipeline(TaskPipeline(
        pipeline_id="p1", session_id="test-rev-002", tasks=[writer_task]
    ))

    new_version = _bump_writer_paper_version(bus)

    assert new_version == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/unit/test_paper_version.py::test_review_rewrite_endpoint_bumps_paper_version -v
```

Expected: **FAIL** with `ImportError: cannot import name '_bump_writer_paper_version' from 'eurekaclaw.ui.server'`.

- [ ] **Step 3: Write minimal implementation**

Edit `eurekaclaw/ui/server.py`. Add the helper near the other module-level helpers (around line 1260, above `_extract_latex_error`):

```python
def _bump_writer_paper_version(bus) -> int:
    """Increment writer.outputs['paper_version'] on the bus pipeline.

    Treats a missing field as version 1 (the frontend shows v1 for
    first writer output). Returns the new version. No-op (returns 0) if
    no writer task is found.
    """
    pipeline = bus.get_pipeline()
    if not pipeline:
        return 0
    writer_task = next(
        (t for t in pipeline.tasks if t.name == "writer"), None
    )
    if writer_task is None:
        return 0
    outputs = writer_task.outputs or {}
    new_version = int(outputs.get("paper_version", 1)) + 1
    outputs["paper_version"] = new_version
    writer_task.outputs = outputs
    bus.put_pipeline(pipeline)
    return new_version
```

Then edit the `/review/rewrite` success branch at line 2125-2143 to call it:

```python
                if new_latex:
                    bus.persist(session_dir)
                    # Bump writer.outputs.paper_version so the polling
                    # frontend picks up the new version.
                    _bump_writer_paper_version(bus)
                    # Write paper.tex to both session dir and output dir
                    for target_dir in [session_dir, Path(run.output_dir) if run.output_dir else None]:
                        if target_dir and target_dir.is_dir():
                            tex_p = target_dir / "paper.tex"
                            tex_p.write_text(new_latex, encoding="utf-8")
                            # Remove stale PDF so frontend triggers recompilation
                            pdf_p = target_dir / "paper.pdf"
                            if pdf_p.is_file():
                                pdf_p.unlink()
                    # Clean up backup
                    if backup_dir.is_dir():
                        _shutil.rmtree(backup_dir)
                    # Only persist the marker once the rewrite actually
                    # produced a new paper, so rejected or failed-and-
                    # restored attempts don't pollute the history.
                    self._append_paper_qa_rewrite_marker(session_id, revision_prompt)
                    self._send_json({"ok": True})
                else:
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/unit/test_paper_version.py tests/unit/test_paper_qa_handler.py -v
```

Expected: all tests PASS.

Also run the full suite quickly to check nothing else broke:

```bash
.venv/bin/python -m pytest tests/unit/ -v -x
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
git add eurekaclaw/agents/writer/agent.py \
        eurekaclaw/orchestrator/paper_qa_handler.py \
        eurekaclaw/ui/server.py \
        tests/unit/test_paper_version.py \
        tests/unit/test_paper_qa_handler.py
git commit -m "$(cat <<'EOF'
feat: add paper_version field on writer outputs

Writer sets paper_version=1 on first run. PaperQAHandler (gate path) and
/review/rewrite both bump writer.outputs.paper_version by 1 on successful
rewrite via the shared _bump_writer_paper_version(bus) helper. Failed
rewrites leave the counter untouched.

Frontend hook (upcoming) reads this field directly instead of deriving
version from rewrite-marker chat messages.
EOF
)"
```

Verify:

```bash
git status
```

Expected: clean working tree, one new commit on `shiyuan/paper-qa-gate`.

---

## Task 2 — Backend: `_sync_latex_to_disk` helper + `REWRITE_MARKER_PREFIX` constant

**Goal:** Dedup the `bus-pipeline → paper.tex` sync code that appears in `/compile-pdf` and `/artifacts/paper.tex` into one helper; hoist the rewrite marker string literal into a shared constant.

**Files:**
- Create: `eurekaclaw/ui/constants.py` — `REWRITE_MARKER_PREFIX`.
- Create: `tests/unit/test_sync_latex_to_disk.py`.
- Modify: `eurekaclaw/ui/server.py` — add helper, use it in two handlers, use constant in `_append_paper_qa_rewrite_marker`.
- Modify: `eurekaclaw/orchestrator/paper_qa_handler.py` — use constant in gate-rewrite marker construction.

---

### Task 2.1 — Add `REWRITE_MARKER_PREFIX` constant

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_rewrite_marker_constant.py`:

```python
"""Tests for the shared REWRITE_MARKER_PREFIX constant."""


def test_rewrite_marker_prefix_exact_value():
    from eurekaclaw.ui.constants import REWRITE_MARKER_PREFIX

    assert REWRITE_MARKER_PREFIX == '↻ Rewrite requested: '


def test_rewrite_marker_prefix_used_in_server():
    """server.py._append_paper_qa_rewrite_marker writes entries whose
    content starts with REWRITE_MARKER_PREFIX — enforced by grep."""
    import pathlib, re
    src = pathlib.Path("eurekaclaw/ui/server.py").read_text(encoding="utf-8")
    # The literal "↻ Rewrite requested: " must appear only via the
    # constant — no more open-coded f-strings in this file.
    open_coded = re.findall(r'["\']↻ Rewrite requested:', src)
    assert open_coded == [], (
        f"server.py still contains open-coded rewrite markers: {open_coded}"
    )
    assert "REWRITE_MARKER_PREFIX" in src


def test_rewrite_marker_prefix_used_in_paper_qa_handler():
    import pathlib, re
    src = pathlib.Path("eurekaclaw/orchestrator/paper_qa_handler.py").read_text(encoding="utf-8")
    open_coded = re.findall(r'["\']↻ Rewrite requested:', src)
    assert open_coded == [], (
        f"paper_qa_handler.py still contains open-coded rewrite markers: {open_coded}"
    )
    assert "REWRITE_MARKER_PREFIX" in src
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/unit/test_rewrite_marker_constant.py -v
```

Expected: all 3 tests FAIL (`ModuleNotFoundError: No module named 'eurekaclaw.ui.constants'` for the first, open-coded strings present for the other two).

- [ ] **Step 3: Write minimal implementation**

Create `eurekaclaw/ui/constants.py`:

```python
"""Shared UI-layer constants used by both the HTTP server and the
orchestrator-side Paper QA handler."""

REWRITE_MARKER_PREFIX = '↻ Rewrite requested: '
```

Edit `eurekaclaw/ui/server.py` — add import near the top (with other `from eurekaclaw...` imports, around line 29-34):

```python
from eurekaclaw.ui.constants import REWRITE_MARKER_PREFIX
```

Replace the open-coded f-string in `_append_paper_qa_rewrite_marker` (line 2370):

```python
            entry = {
                "role": "system",
                "content": f'{REWRITE_MARKER_PREFIX}"{prompt}"',
                "ts": _dt.now(_tz.utc).isoformat(),
            }
```

Edit `eurekaclaw/orchestrator/paper_qa_handler.py` — add import at the top (with other `from eurekaclaw.` imports near line 1-20):

```python
from eurekaclaw.ui.constants import REWRITE_MARKER_PREFIX
```

Replace the open-coded f-string at line 189:

```python
                    marker = {
                        "role": "system",
                        "content": f'{REWRITE_MARKER_PREFIX}"{decision.question}"',
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "version": self._paper_version,
                    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest tests/unit/test_rewrite_marker_constant.py tests/unit/test_paper_qa_handler.py -v
```

Expected: all 3 new tests PASS, existing paper_qa_handler tests still PASS.

---

### Task 2.2 — Extract `_sync_latex_to_disk(run)` helper

**Context:** `/compile-pdf` (server.py:1862-1883) and `/artifacts/paper.tex` (server.py:1608-1622) both do: read writer task bus outputs → if latex differs from on-disk `paper.tex`, write it. `/compile-pdf` *additionally* deletes `paper.pdf` on change; `/artifacts` does not. Factor out the shared read-write-if-different logic; keep the PDF-delete in the `/compile-pdf` handler itself (the spec is explicit that the helper does not touch PDF).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_sync_latex_to_disk.py`:

```python
"""Tests for the _sync_latex_to_disk(run) helper."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
    eureka_session_id: str = "test-sync-001"


@pytest.fixture
def run_with_bus(tmp_path):
    bus = KnowledgeBus("test-sync-001")
    writer = Task(
        task_id="w1",
        name="writer",
        agent_role="writer",
        description="Write paper",
        status=TaskStatus.COMPLETED,
        outputs={"latex_paper": r"\section{Memory} v1"},
    )
    bus.put_pipeline(TaskPipeline(
        pipeline_id="p1", session_id="test-sync-001", tasks=[writer],
    ))
    session = _FakeSession(bus=bus)
    output_dir = tmp_path / "run-output"
    output_dir.mkdir()
    return _FakeRun(output_dir=str(output_dir), eureka_session=session), output_dir, bus


def test_sync_writes_paper_tex_when_disk_empty(run_with_bus):
    from eurekaclaw.ui.server import _sync_latex_to_disk

    run, output_dir, _bus = run_with_bus
    tex_path = output_dir / "paper.tex"
    assert not tex_path.exists()

    changed, latex = _sync_latex_to_disk(run)

    assert changed is True
    assert latex == r"\section{Memory} v1"
    assert tex_path.read_text(encoding="utf-8") == r"\section{Memory} v1"


def test_sync_no_change_when_disk_matches(run_with_bus):
    from eurekaclaw.ui.server import _sync_latex_to_disk

    run, output_dir, _bus = run_with_bus
    tex_path = output_dir / "paper.tex"
    tex_path.write_text(r"\section{Memory} v1", encoding="utf-8")

    changed, latex = _sync_latex_to_disk(run)

    assert changed is False
    assert latex == r"\section{Memory} v1"


def test_sync_writes_when_bus_differs_from_disk(run_with_bus):
    from eurekaclaw.ui.server import _sync_latex_to_disk

    run, output_dir, bus = run_with_bus
    tex_path = output_dir / "paper.tex"
    tex_path.write_text(r"\section{Old}", encoding="utf-8")

    # Mutate bus state to simulate a rewrite landing in memory.
    pipeline = bus.get_pipeline()
    pipeline.tasks[0].outputs["latex_paper"] = r"\section{Memory} v2"
    bus.put_pipeline(pipeline)

    changed, latex = _sync_latex_to_disk(run)

    assert changed is True
    assert latex == r"\section{Memory} v2"
    assert tex_path.read_text(encoding="utf-8") == r"\section{Memory} v2"


def test_sync_never_touches_paper_pdf(run_with_bus):
    """The helper must not unlink paper.pdf even when .tex changes."""
    from eurekaclaw.ui.server import _sync_latex_to_disk

    run, output_dir, bus = run_with_bus
    tex_path = output_dir / "paper.tex"
    tex_path.write_text("old", encoding="utf-8")
    pdf_path = output_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.5\nstub")

    pipeline = bus.get_pipeline()
    pipeline.tasks[0].outputs["latex_paper"] = "new"
    bus.put_pipeline(pipeline)

    _sync_latex_to_disk(run)

    assert pdf_path.exists()
    assert pdf_path.read_bytes() == b"%PDF-1.5\nstub"


def test_sync_no_bus_returns_false_empty(tmp_path):
    from eurekaclaw.ui.server import _sync_latex_to_disk

    output_dir = tmp_path / "x"
    output_dir.mkdir()
    run = _FakeRun(output_dir=str(output_dir), eureka_session=None)

    changed, latex = _sync_latex_to_disk(run)

    assert changed is False
    assert latex == ""


def test_sync_no_output_dir_returns_false_empty(tmp_path):
    from eurekaclaw.ui.server import _sync_latex_to_disk

    bus = KnowledgeBus("no-dir")
    session = _FakeSession(bus=bus)
    run = _FakeRun(output_dir=None, eureka_session=session)

    changed, latex = _sync_latex_to_disk(run)

    assert changed is False
    assert latex == ""
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/unit/test_sync_latex_to_disk.py -v
```

Expected: all 6 tests FAIL with `ImportError: cannot import name '_sync_latex_to_disk' from 'eurekaclaw.ui.server'`.

- [ ] **Step 3: Write minimal implementation**

Edit `eurekaclaw/ui/server.py`. Add the helper near `_bump_writer_paper_version` (around line 1260, right above `_extract_latex_error`):

```python
def _sync_latex_to_disk(run) -> tuple[bool, str]:
    """Sync writer bus latex → <run.output_dir>/paper.tex.

    Returns (changed, latex). Writes paper.tex only if bus latex differs
    from the on-disk copy. Never touches paper.pdf — callers that care
    about stale PDFs are responsible for unlinking them.
    """
    if not getattr(run, "output_dir", None):
        return False, ""
    session = getattr(run, "eureka_session", None)
    bus = getattr(session, "bus", None) if session else None
    if bus is None:
        return False, ""
    pipeline = bus.get_pipeline()
    if not pipeline:
        return False, ""
    writer = next((t for t in pipeline.tasks if t.name == "writer"), None)
    if writer is None or not writer.outputs:
        return False, ""
    latex = writer.outputs.get("latex_paper", "")
    if not latex:
        return False, ""
    tex_path = Path(run.output_dir) / "paper.tex"
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    old = tex_path.read_text(encoding="utf-8") if tex_path.is_file() else ""
    if latex != old:
        tex_path.write_text(latex, encoding="utf-8")
        return True, latex
    return False, latex
```

Replace the `/compile-pdf` bus-sync block at lines 1866-1883:

```python
            tex_path = Path(run.output_dir) / "paper.tex"
            # Always sync the latest LaTeX from the writer task's
            # in-memory output to disk. At gate time paper.tex may not
            # exist yet, and after a rewrite the on-disk copy is stale.
            changed, _ = _sync_latex_to_disk(run)
            if changed:
                # Remove stale PDF so it gets freshly compiled.
                stale_pdf = Path(run.output_dir) / "paper.pdf"
                if stale_pdf.is_file():
                    stale_pdf.unlink()
            if not tex_path.is_file():
                self._send_json({"error": "No paper.tex found"}, status=HTTPStatus.BAD_REQUEST)
                return
```

Replace the `/artifacts` paper.tex sync block at lines 1608-1622:

```python
            # Sync the latest paper.tex from memory so .tex downloads
            # reflect any edits made since save_artifacts last ran.
            # Never touch paper.pdf here — compile-pdf owns the PDF
            # lifecycle.
            if _art_filename == "paper.tex":
                _sync_latex_to_disk(_art_run)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/unit/test_sync_latex_to_disk.py tests/unit/test_rewrite_marker_constant.py -v
```

Expected: all PASS.

Smoke-check the server file still parses cleanly:

```bash
.venv/bin/python -c "import eurekaclaw.ui.server; print('OK')"
```

Expected: `OK`.

Full suite:

```bash
.venv/bin/python -m pytest tests/unit/ -v -x
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add eurekaclaw/ui/constants.py \
        eurekaclaw/ui/server.py \
        eurekaclaw/orchestrator/paper_qa_handler.py \
        tests/unit/test_sync_latex_to_disk.py \
        tests/unit/test_rewrite_marker_constant.py
git commit -m "$(cat <<'EOF'
refactor: extract _sync_latex_to_disk helper and share REWRITE_MARKER_PREFIX

Consolidates the bus→paper.tex sync logic (previously duplicated in
/compile-pdf and /artifacts GET) into one helper on the server module.
Helper never touches paper.pdf; callers own the PDF lifecycle so the
artifacts GET handler can't regress the "delete-on-GET races the
iframe" class of bugs.

Hoists the '↻ Rewrite requested: ' string literal into
eurekaclaw.ui.constants.REWRITE_MARKER_PREFIX so server.py and
paper_qa_handler.py stay in sync; the frontend pulls the same value
from frontend/src/constants/paper.ts (added in Task 3).
EOF
)"
```

---

## Task 3 — Frontend: `usePaperSession` hook + vitest suite

**Goal:** Add a state-machine hook that encapsulates all Paper-session React state. Ship a vitest suite that locks the interface down. Not wired into any component yet — purely additive.

**Files:**
- Modify: `frontend/package.json` (add devDeps, `test` script).
- Modify: `frontend/vite.config.ts` (add `test` config).
- Create: `frontend/src/test-setup.ts`.
- Create: `frontend/src/constants/paper.ts`.
- Create: `frontend/src/components/workspace/paper-review/usePaperSession.ts`.
- Create: `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`.

---

### Task 3.1 — Install vitest + test dependencies

- [ ] **Step 1: Install dependencies**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm install --save-dev vitest@^2.1.9 \
                       jsdom@^25.0.0 \
                       @testing-library/react@^16.1.0 \
                       @testing-library/jest-dom@^6.6.3 \
                       @types/react-test-renderer@^18.3.1
```

Expected: `package.json` gains those five packages in `devDependencies`; `package-lock.json` updates.

- [ ] **Step 2: Add `test` script**

Edit `frontend/package.json` — replace the `scripts` block:

```json
  "scripts": {
    "dev": "vite",
    "dev:all": "concurrently -n backend,frontend -c cyan,magenta \"eurekaclaw ui --port 7860\" \"vite\"",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "test:watch": "vitest"
  },
```

- [ ] **Step 3: Configure vitest in vite.config.ts**

Replace `frontend/vite.config.ts` entirely:

```ts
/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../eurekaclaw/ui/static', import.meta.url)),
    emptyOutDir: true,
  },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
```

- [ ] **Step 4: Add test setup file**

Create `frontend/src/test-setup.ts`:

```ts
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});
```

- [ ] **Step 5: Smoke-check vitest runs**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npx vitest run --reporter=basic
```

Expected: `No test files found` (zero tests yet, but vitest itself runs clean — exit code 0 with the `--passWithNoTests` implicit behavior or 1 if vitest requires at least one file; either is acceptable as long as it's the *right* error).

If vitest exits nonzero on "no tests", that's fine — the next step adds a test.

---

### Task 3.2 — Add `REWRITE_MARKER_PREFIX` constant on the TS side

- [ ] **Step 1: Create the constant**

Create `frontend/src/constants/paper.ts`:

```ts
/**
 * Prefix used in chat history to mark a rewrite request. Must stay in
 * sync with `eurekaclaw.ui.constants.REWRITE_MARKER_PREFIX` — both
 * sides build the full marker as `${PREFIX}"${question}"`.
 */
export const REWRITE_MARKER_PREFIX = '↻ Rewrite requested: ';
```

- [ ] **Step 2: No test needed yet** — the constant is exercised by the hook tests in the next task. Move on.

---

### Task 3.3 — First hook test: no-paper mode

**Context:** Follow TDD: write the smallest failing test, then the minimal hook that passes it, then incrementally add more tests.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/workspace/paper-review/usePaperSession.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { SessionRun, PipelineTask } from '@/types';

// Mock the API client before importing the hook.
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
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npx vitest run usePaperSession
```

Expected: **FAIL** with module-resolution error — `usePaperSession.ts` doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/components/workspace/paper-review/usePaperSession.ts`:

```ts
import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost } from '@/api/client';
import { REWRITE_MARKER_PREFIX } from '@/constants/paper';
import type { SessionRun, QAMessage, PipelineTask } from '@/types';

export type PaperMode =
  | 'no-paper'
  | 'loading-review'
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

  reviewError: string | null;
  isHistorical: boolean;
}

type HistoryResponse = { messages: QAMessage[] };

export function usePaperSession(run: SessionRun | null): PaperSession | null {
  const [messages, setMessages] = useState<QAMessage[]>([]);
  const [reviewStatus, setReviewStatus] = useState<
    'idle' | 'loading' | 'ready' | 'failed'
  >('idle');
  const [reviewError, setReviewError] = useState<string | null>(null);
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
    theoryTask?.status === 'running' ||
    theoryTask?.status === 'pending' ||
    writerTask?.status === 'in_progress' ||
    writerTask?.status === 'running' ||
    writerTask?.status === 'pending';

  const mode: PaperMode = useMemo(() => {
    if (!run) return 'no-paper';
    if (run.status === 'failed' && !hasPaper) return 'failed';
    if (!hasPaper) return 'no-paper';
    if (paperQATask?.status === 'awaiting_gate') return 'gate';
    if (paperQATask?.status === 'completed' && pipelineRewriting) return 'rewriting';
    if (reviewStatus !== 'ready') return 'loading-review';
    return 'completed';
  }, [
    run,
    hasPaper,
    paperQATask?.status,
    pipelineRewriting,
    reviewStatus,
  ]);

  // Effect 1: activate review (load bus server-side) for historical completed runs.
  useEffect(() => {
    if (!run?.run_id) return;
    if (mode !== 'completed' && mode !== 'loading-review') return;
    if (reviewStatus !== 'idle') return;
    setReviewStatus('loading');
    void (async () => {
      try {
        await apiPost(`/api/runs/${run.run_id}/review`, {});
        setReviewStatus('ready');
        setReviewError(null);
      } catch (e) {
        setReviewStatus('failed');
        setReviewError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [run?.run_id, mode, reviewStatus]);

  // Effect 2: load history once bus is ready OR during gate/rewrite (bus is in-memory).
  useEffect(() => {
    if (!run?.run_id) return;
    const canLoad =
      reviewStatus === 'ready' || mode === 'gate' || mode === 'rewriting';
    if (!canLoad) return;
    void (async () => {
      try {
        const data = await apiGet<HistoryResponse>(
          `/api/runs/${run.run_id}/paper-qa/history`,
        );
        setMessages(data.messages ?? []);
      } catch {
        setMessages([]);
      }
    })();
  }, [run?.run_id, reviewStatus, mode]);

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
        if (mode === 'gate') {
          await apiPost(`/api/runs/${run.run_id}/gate/paper_qa`, {
            action: 'rewrite',
            question: prompt,
          });
        } else {
          await apiPost(`/api/runs/${run.run_id}/review/rewrite`, {
            revision_prompt: prompt,
          });
          // Trigger bus reactivation so the hook picks up fresh artifacts.
          setReviewStatus('idle');
        }
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
    [run?.run_id, mode],
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
    reviewError,
    isHistorical,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run usePaperSession
```

Expected: 2 tests PASS.

---

### Task 3.4 — Add coverage for gate mode

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/components/workspace/paper-review/usePaperSession.test.ts` (inside the same `describe('usePaperSession', ...)` block):

```ts
  it('enters gate mode when paper_qa_gate awaits gate and writer has output', async () => {
    apiGetMock.mockResolvedValue({ messages: [] });
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.mode).toBe('gate');
    expect(result.current?.isHistorical).toBe(false);
    // History is loaded because the bus is in-memory during a gate.
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith(
      '/api/runs/run-1/paper-qa/history',
    ));
  });

  it('onAccept posts no-action to the gate endpoint in gate mode', async () => {
    apiGetMock.mockResolvedValue({ messages: [] });
    apiPostMock.mockResolvedValue({ ok: true });
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
```

- [ ] **Step 2: Run tests**

```bash
npx vitest run usePaperSession
```

Expected: all 4 tests PASS (the hook already handles these cases from the first implementation).

---

### Task 3.5 — Rewriting mode + optimistic marker + paperVersion fallback

- [ ] **Step 1: Write the failing test**

Append inside the same `describe` block:

```ts
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

  it('onRewrite in completed mode POSTs to /review/rewrite and appends optimistic marker', async () => {
    apiGetMock.mockResolvedValue({ messages: [] });
    apiPostMock.mockResolvedValue({ ok: true });
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('completed')],
    });
    const { result, rerender } = renderHook(() => usePaperSession(run));

    // Wait for review activation + history load so reviewStatus=ready.
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/runs/run-1/review', {}),
    );
    rerender();

    await act(async () => {
      await result.current!.onRewrite('fix Section 3');
    });

    // Optimistic marker appended.
    const sysMsg = result.current!.messages.find(
      (m) => m.role === 'system' && m.content.includes('fix Section 3'),
    );
    expect(sysMsg).toBeDefined();
    expect(sysMsg!.content).toBe('↻ Rewrite requested: "fix Section 3"');

    // Rewrite endpoint (not gate endpoint) was called.
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/review/rewrite',
      { revision_prompt: 'fix Section 3' },
    );
  });

  it('paperVersion reads writer.outputs.paper_version when present', () => {
    const run = makeRun({ pipeline: [writerTask(3), paperQATask('completed')] });
    const { result } = renderHook(() => usePaperSession(run));
    expect(result.current?.paperVersion).toBe(3);
  });

  it('paperVersion falls back to 1 + rewrite-marker count when writer lacks the field', () => {
    // Writer without paper_version (simulating pre-migration runs).
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
        {
          role: 'system',
          content: '↻ Rewrite requested: "round 1"',
          ts: '2026-04-17T00:00:00Z',
        },
        {
          role: 'system',
          content: '↻ Rewrite requested: "round 2"',
          ts: '2026-04-17T01:00:00Z',
        },
      ],
    });
    const { result } = renderHook(() => usePaperSession(run));
    return waitFor(() => {
      expect(result.current?.paperVersion).toBe(3);
    });
  });

  it('onRewrite in gate mode POSTs to /gate/paper_qa with rewrite action', async () => {
    apiGetMock.mockResolvedValue({ messages: [] });
    apiPostMock.mockResolvedValue({ ok: true });
    const run = makeRun({
      pipeline: [writerTask(1), paperQATask('awaiting_gate')],
    });
    const { result } = renderHook(() => usePaperSession(run));

    await act(async () => {
      await result.current!.onRewrite('retry proof');
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/runs/run-1/gate/paper_qa',
      { action: 'rewrite', question: 'retry proof' },
    );
  });
```

- [ ] **Step 2: Run tests**

```bash
npx vitest run usePaperSession
```

Expected: all 9 tests PASS. The hook already satisfies these because Task 3.3 wrote the full implementation. If any fail, fix the hook — the tests are authoritative.

- [ ] **Step 3: Run full frontend checks**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm run typecheck
npm run test
```

Expected: `typecheck` passes (no `tsc` errors), `test` passes (9/9).

- [ ] **Step 4: Commit**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
git add frontend/package.json \
        frontend/package-lock.json \
        frontend/vite.config.ts \
        frontend/src/test-setup.ts \
        frontend/src/constants/paper.ts \
        frontend/src/components/workspace/paper-review/usePaperSession.ts \
        frontend/src/components/workspace/paper-review/usePaperSession.test.ts
git commit -m "$(cat <<'EOF'
feat(frontend): add usePaperSession hook + vitest suite

New hook encapsulates all Paper-session React state: review activation,
history load, mode derivation (no-paper → gate → rewriting → completed),
optimistic rewrite markers, and gate-vs-historical endpoint dispatch.
Not wired to any component yet — Task 4 swaps PaperPanel to use it.

Also adds vitest + jsdom + @testing-library/react (first vitest suite in
this repo), npm test script, and the TS-side REWRITE_MARKER_PREFIX
constant mirroring eurekaclaw.ui.constants.
EOF
)"
```

---

## Task 4 — Frontend: merge `PaperPanel` + delete `PaperReviewPanel` + simplify `WorkspaceTabs`

**Goal:** `PaperPanel` becomes the only Paper-session component and consumes `usePaperSession`. `PaperReviewPanel.tsx` is deleted. `WorkspaceTabs` drops the gate/rewrite takeover fork so the user can tab-switch freely during a gate.

**Files:**
- Rewrite: `frontend/src/components/workspace/PaperPanel.tsx`.
- Delete: `frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx`.
- Modify: `frontend/src/components/workspace/WorkspaceTabs.tsx`.
- Modify: `frontend/src/styles/paper-review.css` (remove unused `.review-divider-static` rules).

---

### Task 4.1 — Rewrite `PaperPanel` to use the hook

- [ ] **Step 1: Replace file contents**

Replace `frontend/src/components/workspace/PaperPanel.tsx` entirely:

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionRun } from '@/types';
import { PaperViewer } from './paper-review/PaperViewer';
import { QAChat } from './paper-review/QAChat';
import { usePaperSession } from './paper-review/usePaperSession';

interface PaperPanelProps {
  run: SessionRun | null;
}

const SPLIT_KEY = 'eurekaclaw-review-split';
const MIN_SPLIT = 30;
const MAX_SPLIT = 70;
const DEFAULT_SPLIT = 55;

function loadInitialSplit(): number {
  const saved = localStorage.getItem(SPLIT_KEY);
  if (!saved) return DEFAULT_SPLIT;
  const parsed = parseFloat(saved);
  if (!Number.isFinite(parsed)) return DEFAULT_SPLIT;
  return Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, parsed));
}

export function PaperPanel({ run }: PaperPanelProps) {
  // Force the hook (and all downstream state) to reset on session switch.
  return <PaperPanelInner key={run?.run_id ?? '__none__'} run={run} />;
}

function PaperPanelInner({ run }: PaperPanelProps) {
  const session = usePaperSession(run);

  const [splitPct, setSplitPct] = useState(loadInitialSplit);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const splitPctRef = useRef(splitPct);
  splitPctRef.current = splitPct;

  const handleMouseDown = useCallback(() => setIsDragging(true), []);

  useEffect(() => {
    if (!isDragging) return;
    function onMouseMove(e: MouseEvent) {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      const clamped = Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, pct));
      setSplitPct(clamped);
    }
    function onMouseUp() {
      setIsDragging(false);
      localStorage.setItem(SPLIT_KEY, String(splitPctRef.current));
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

  // session is non-null whenever run is non-null, but narrow for TS.
  if (!session) {
    return null;
  }

  if (session.mode === 'failed') {
    return (
      <div className="paper-preview">
        <div className="paper-empty-state">
          <p>{run.error || 'The session failed before a paper could be generated.'}</p>
        </div>
      </div>
    );
  }

  if (session.mode === 'no-paper') {
    if (run.status === 'running') {
      return (
        <div className="paper-preview">
          <div className="paper-empty-state">
            <div className="paper-progress-dots">
              <span /><span /><span />
            </div>
            <p>Paper will appear once the writer agent completes.</p>
          </div>
        </div>
      );
    }
    return (
      <div className="paper-preview">
        <div className="paper-empty-state">
          <p>No paper generated yet.</p>
        </div>
      </div>
    );
  }

  if (session.mode === 'loading-review') {
    return (
      <div className="paper-preview">
        <div className="paper-empty-state">
          <div className="paper-progress-dots">
            <span /><span /><span />
          </div>
          <p>Loading paper review...</p>
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
      {session.reviewError ? (
        <div className="paper-review-error-banner">
          Review activation failed: {session.reviewError}
        </div>
      ) : null}

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
        <QAChat
          run={session.run}
          messages={session.messages}
          setMessages={session.setMessages}
          isRewriting={session.isRewriting}
          isHistorical={session.isHistorical}
          onAccept={session.onAccept}
          onRewrite={session.onRewrite}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm run typecheck
```

Expected: PASS with no errors.

If TS complains about `onAccept: () => Promise<void>` not matching `QAChat`'s prop `onAccept: () => void`, widen `QAChat`'s prop:

Open `frontend/src/components/workspace/paper-review/QAChat.tsx`, change lines 12-13:

```ts
  onAccept: () => void | Promise<void>;
  onRewrite: (prompt: string) => void | Promise<void>;
```

Re-run `npm run typecheck`.

---

### Task 4.2 — Delete `PaperReviewPanel.tsx`

- [ ] **Step 1: Delete the file**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
rm frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx
```

- [ ] **Step 2: Confirm no references remain**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
grep -RIn "PaperReviewPanel" src/ || echo "no references"
```

Expected: `no references` (anything remaining in `WorkspaceTabs.tsx` is handled in Task 4.3).

Actually, at this point `WorkspaceTabs.tsx` still imports `PaperReviewPanel` — TS won't compile. Proceed to 4.3 before re-running typecheck.

---

### Task 4.3 — Simplify `WorkspaceTabs.tsx`

- [ ] **Step 1: Replace file contents**

Replace `frontend/src/components/workspace/WorkspaceTabs.tsx` entirely:

```tsx
import { useUiStore } from '@/store/uiStore';
import { LivePanel } from './LivePanel';
import { ProofPanel } from './ProofPanel';
import { PaperPanel } from './PaperPanel';
import type { SessionRun } from '@/types';

interface WorkspaceTabsProps {
  run: SessionRun | null;
}

const TABS = [
  { key: 'live', label: 'Live' },
  { key: 'proof', label: 'Proof' },
  { key: 'paper', label: 'Paper' },
] as const;

type TabKey = typeof TABS[number]['key'];

export function WorkspaceTabs({ run }: WorkspaceTabsProps) {
  const activeWsTab = useUiStore((s) => s.activeWsTab);
  const setActiveWsTab = useUiStore((s) => s.setActiveWsTab);

  return (
    <div className="workspace-main-col">
      <div className="ws-tab-bar" role="tablist" aria-label="Workspace views">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={`ws-tab${activeWsTab === tab.key ? ' is-active' : ''}`}
            data-ws-tab={tab.key}
            role="tab"
            aria-selected={activeWsTab === tab.key}
            aria-controls={`ws-panel-${tab.key}`}
            onClick={() => setActiveWsTab(tab.key as TabKey)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className={`ws-panel${activeWsTab === 'live' ? ' is-visible' : ''}`} id="ws-panel-live" role="tabpanel">
        <LivePanel run={run} />
      </div>
      <div className={`ws-panel${activeWsTab === 'proof' ? ' is-visible' : ''}`} id="ws-panel-proof" role="tabpanel">
        <ProofPanel run={run} />
      </div>
      <div className={`ws-panel${activeWsTab === 'paper' ? ' is-visible' : ''}`} id="ws-panel-paper" role="tabpanel">
        <PaperPanel run={run} />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm run typecheck
```

Expected: PASS.

---

### Task 4.4 — Clean up unused `.review-divider-static` CSS

- [ ] **Step 1: Remove CSS block**

Open `frontend/src/styles/paper-review.css` and delete the three rules added during the prior "static divider" patch:

```css
.review-divider-static {
  cursor: default;
  background: var(--line);
}
.review-divider-static:hover { background: var(--line); }
.review-divider-static::after { content: none; }
```

(If you're unsure where they are, `grep -n review-divider-static` inside the file first.)

- [ ] **Step 2: Confirm build still succeeds**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm run build
```

Expected: Vite build completes, no "CSS import not found" errors.

---

### Task 4.5 — Run full frontend check + commit

- [ ] **Step 1: Typecheck + tests**

```bash
cd /Users/chenggong/Downloads/EurekaClaw/frontend
npm run typecheck
npm run test
```

Expected: both PASS.

- [ ] **Step 2: Commit**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
git add frontend/src/components/workspace/PaperPanel.tsx \
        frontend/src/components/workspace/WorkspaceTabs.tsx \
        frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx \
        frontend/src/components/workspace/paper-review/QAChat.tsx \
        frontend/src/styles/paper-review.css
# (the rm above already staged the delete via git add on the path)
git status  # verify deletion is staged
git commit -m "$(cat <<'EOF'
refactor(frontend): collapse PaperPanel + PaperReviewPanel into one hook-driven panel

PaperPanel is now the sole Paper-session component and consumes the
usePaperSession hook for all state. PaperReviewPanel.tsx is deleted.

WorkspaceTabs drops the gate/rewrite "panel takeover" fork — PaperPanel
is always rendered in the paper tab slot, so the user can switch
between Live/Proof/Paper tabs freely even during an active gate or
in-flight rewrite. Tab bar stays visible in all modes.

One draggable divider (30–70% split, persisted to localStorage) is
shared across all Paper modes. The short-lived static-divider CSS added
during the "delete-on-GET" triage is removed.
EOF
)"
```

---

## Task 5 — Manual verification

**Goal:** Walk the spec's checklist in a running instance. No code changes — if any item fails, open a fix in a follow-up commit.

- [ ] **Step 1: Start backend + frontend**

```bash
cd /Users/chenggong/Downloads/EurekaClaw
# Terminal A:
eurekaclaw ui --port 7860

# Terminal B:
cd frontend && npm run dev
```

Open http://127.0.0.1:5173 in Chrome.

- [ ] **Step 2: Fresh session → gate → QA → Rewrite → new version → Accept**

1. Start a new research session.
2. Wait for writer to complete; paper gate appears.
3. Confirm Paper tab shows the PDF/LaTeX viewer with "Paper v1" and the QA chat on the right.
4. Ask a question; confirm assistant response arrives.
5. Click "↻ Revise Paper", submit a prompt.
6. Confirm the system message "↻ Rewrite requested: …" appears in chat.
7. Confirm mode transitions to `rewriting` (viewer shows "Rewriting…" state, version unchanged until new writer output lands).
8. When rewrite finishes, confirm viewer refreshes and version displays `v2`.
9. Click "Accept"; confirm gate closes cleanly.

Expected: all transitions work without console errors.

- [ ] **Step 3: Browser refresh in each mode**

For each of `gate`, `rewriting`, `completed`:
1. Reach that mode via the UI.
2. Hit Cmd+R.
3. Confirm: QA messages restored, PDF loads, paper version correct.

- [ ] **Step 4: Session switch**

1. Open sidebar, switch between two completed sessions with different QA histories.
2. Confirm messages and viewer update correctly each time (no stale messages carry over).

- [ ] **Step 5: Gate-period tab switch**

1. Start a session, wait for gate.
2. Switch to Live tab, then Proof, then back to Paper.
3. Confirm the tab bar stays visible throughout; confirm QA messages are still there when you return.

- [ ] **Step 6: PDF compile failure surfaces log tail**

1. Manually corrupt the LaTeX (e.g. remove `\end{document}` via `paper.tex` download-edit-upload-cycle, or run a session with a known-bad template).
2. Click "Compile PDF".
3. Confirm the error banner includes the first `! LaTeX Error:` line from `paper.log`, not just "check paper.log".

- [ ] **Step 7: Failed session**

1. Force a failure (e.g. invalid API key for writer).
2. Click on the failed session in the sidebar.
3. Confirm Paper tab renders "The session failed before a paper could be generated." without crashing.

- [ ] **Step 8: Old runs without `paper_version`**

1. Pick a session folder in `~/.eurekaclaw/runs/<old_session_id>/` from before Task 1 shipped.
2. Load it in the UI (`eurekaclaw review <old_session_id>` or the "Open" button).
3. Confirm the paper version label falls back to `1 + <rewrite marker count>` as expected.

- [ ] **Step 9: Report results**

If every item above passes, write one line confirming in the PR description.
If anything fails, open a follow-up commit referencing the failure; do not amend prior commits.

---

## Self-Review Checklist (executed before delivering this plan)

1. **Spec coverage:**
   - (A) `paper_version` field → Task 1 (three sub-tasks, one per writer/handler/endpoint). ✔
   - (B) `_sync_latex_to_disk` helper → Task 2.2. ✔
   - (C) `REWRITE_MARKER_PREFIX` constant → Task 2.1 (Python) + Task 3.2 (TS). ✔
   - `usePaperSession` hook + vitest → Task 3. ✔
   - `PaperPanel` merge + `PaperReviewPanel` delete + `WorkspaceTabs` simplification → Task 4. ✔
   - Manual verification → Task 5. ✔
   - Error banner for `reviewError` → Task 4.1 (`.paper-review-error-banner`; note: CSS class styling not explicitly written — relies on existing `.paper-empty-state` muted-error pattern; acceptable since the spec's error-handling table calls for "non-blocking banner above PaperViewer" and the element is rendered; styling can be adjusted inline during manual verification if needed).
   - Draggable divider persistence → Task 4.1 (`SPLIT_KEY = 'eurekaclaw-review-split'`, clamped 30–70). ✔
   - `key={run.run_id}` remount → Task 4.1 (`PaperPanel` outer wrapper). ✔
2. **Placeholder scan:** no TBD/TODO/"fill in"/"handle edge cases" tokens remain. ✔
3. **Type consistency:** `PaperMode`, `PaperSession`, `usePaperSession(run)` names consistent across Task 3.3 (implementation) and Task 4.1 (consumer). `_bump_writer_paper_version` signature consistent across Task 1.2 (tested) and Task 1.3 (called). `_sync_latex_to_disk` signature `(run) -> tuple[bool, str]` consistent across Task 2.2 tests and implementation. ✔

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-04-17-paper-session-redesign.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (Task 1, Task 2, Task 3, Task 4), review between tasks, fast iteration. Task 5 (manual verification) happens at the end with you at the keyboard.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, with checkpoints after each commit for your review.

Which approach?
