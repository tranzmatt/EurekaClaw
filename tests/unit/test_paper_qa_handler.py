"""Unit tests for PaperQAHandler — CLI interaction flow."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from eurekaclaw.orchestrator.paper_qa_handler import PaperQAHandler
from eurekaclaw.types.tasks import Task, TaskPipeline, TaskStatus
from eurekaclaw.types.artifacts import ResearchBrief


@pytest.fixture
def handler_setup(bus, session_id, tmp_path):
    """Build a PaperQAHandler with mocked dependencies."""
    writer_task = Task(
        task_id="w1",
        name="writer",
        agent_role="writer",
        description="Write paper",
        status=TaskStatus.COMPLETED,
        outputs={"latex_paper": r"\section{Intro}" + "\nTest paper content."},
    )
    qa_gate_task = Task(
        task_id="g1",
        name="paper_qa_gate",
        agent_role="orchestrator",
        description="Paper QA gate",
    )
    pipeline = TaskPipeline(
        pipeline_id="p1",
        session_id=session_id,
        tasks=[writer_task, qa_gate_task],
    )

    brief = ResearchBrief(
        session_id=session_id,
        input_mode="exploration",
        domain="test",
        query="test query",
    )

    handler = PaperQAHandler(
        bus=bus,
        agents={},
        router=MagicMock(),
        client=MagicMock(),
        tool_registry=MagicMock(),
        skill_injector=MagicMock(),
        memory=MagicMock(),
        gate_controller=MagicMock(),
    )
    # Override session dir to tmp_path for test isolation
    handler._session_dir = tmp_path

    return handler, pipeline, brief


@pytest.mark.asyncio
async def test_skip_when_user_declines(handler_setup):
    handler, pipeline, brief = handler_setup
    with patch.object(handler, "_should_review", return_value=False):
        await handler.run(pipeline, brief)
    # Should save v1 but not enter QA loop
    assert handler._paper_version == 1


@pytest.mark.asyncio
async def test_accept_without_questions(handler_setup):
    handler, pipeline, brief = handler_setup
    with (
        patch.object(handler, "_should_review", return_value=True),
        patch.object(handler, "_display_latex_preview"),
        patch.object(handler, "_prompt_question", return_value=""),
    ):
        await handler.run(pipeline, brief)
    assert handler._paper_version == 1


@pytest.mark.asyncio
async def test_qa_history_persisted(handler_setup):
    handler, pipeline, brief = handler_setup

    # Simulate: user asks one question, then accepts
    question_calls = iter(["What is the main result?", ""])
    choice_calls = iter(["a"])

    with (
        patch.object(handler, "_should_review", return_value=True),
        patch.object(handler, "_display_latex_preview"),
        patch.object(handler, "_prompt_question", side_effect=lambda: next(question_calls)),
        patch.object(handler, "_ask_qa_agent", new_callable=AsyncMock, return_value="The main result is..."),
        patch.object(handler, "_prompt_after_answer", side_effect=lambda: next(choice_calls)),
    ):
        await handler.run(pipeline, brief)

    # Check history file was written
    history_file = handler._session_dir / "paper_qa_history.jsonl"
    assert history_file.exists()
    lines = history_file.read_text().strip().split("\n")
    assert len(lines) == 2  # one user turn + one assistant turn
    assert json.loads(lines[0])["role"] == "user"
    assert json.loads(lines[1])["role"] == "assistant"


@pytest.mark.asyncio
async def test_paper_version_saved(handler_setup):
    handler, pipeline, brief = handler_setup
    with patch.object(handler, "_should_review", return_value=False):
        await handler.run(pipeline, brief)
    v1_file = handler._session_dir / "paper_v1.tex"
    assert v1_file.exists()
    assert "Test paper content" in v1_file.read_text()


@pytest.mark.asyncio
async def test_no_latex_skips_gracefully(handler_setup):
    handler, pipeline, brief = handler_setup
    # Remove latex from writer outputs
    pipeline.tasks[0].outputs = {}
    with patch.object(handler, "_should_review", return_value=True):
        await handler.run(pipeline, brief)
    assert handler._paper_version == 0


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


@pytest.mark.asyncio
async def test_do_rewrite_includes_experiment_in_task_list(handler_setup, monkeypatch):
    """_do_rewrite must replay ['theory', 'experiment', 'writer'] when not writer_only."""
    handler, pipeline, brief = handler_setup

    # Add theory + experiment tasks alongside the existing writer.
    from eurekaclaw.types.tasks import Task, TaskStatus
    theory_task = Task(
        task_id="t1", name="theory", agent_role="theory",
        description="Prove theorem",
        status=TaskStatus.COMPLETED,
        outputs={"proof": "Q.E.D."},
    )
    experiment_task = Task(
        task_id="e1", name="experiment", agent_role="experiment",
        description="Run experiment",
        status=TaskStatus.COMPLETED,
        outputs={"metrics": {"acc": 0.9}},
    )
    pipeline.tasks = [theory_task, experiment_task] + pipeline.tasks

    executed_names = []

    class _FakeResult:
        def __init__(self, name):
            self.failed = False
            self.output = {f"{name}_done": True}
            self.text_summary = ""
            self.error = ""

    async def _fake_execute(task):
        executed_names.append(task.name)
        return _FakeResult(task.name)

    fake_agent = MagicMock()
    fake_agent.execute = AsyncMock(side_effect=_fake_execute)
    handler.router.resolve = MagicMock(return_value=fake_agent)

    # Stub out context helpers that write to disk.
    monkeypatch.setattr(handler, "_save_rewrite_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(handler, "_summarize_qa_history", lambda: "")

    result = await handler._do_rewrite(pipeline, brief, revision_prompt="tighten proof")

    assert result is not None
    assert executed_names == ["theory", "experiment", "writer"]


@pytest.mark.asyncio
async def test_do_rewrite_restores_experiment_outputs_on_failure(handler_setup, monkeypatch):
    """When the writer step fails after experiment rewrote, experiment must
    be restored to COMPLETED with its previous outputs, not left PENDING."""
    handler, pipeline, brief = handler_setup

    from eurekaclaw.types.tasks import Task, TaskStatus
    theory_task = Task(
        task_id="t1", name="theory", agent_role="theory",
        description="Prove", status=TaskStatus.COMPLETED,
        outputs={"proof": "original"},
    )
    experiment_task = Task(
        task_id="e1", name="experiment", agent_role="experiment",
        description="Run", status=TaskStatus.COMPLETED,
        outputs={"metrics": {"acc": 0.9}},
    )
    pipeline.tasks = [theory_task, experiment_task] + pipeline.tasks

    class _OkResult:
        def __init__(self, name):
            self.failed = False
            self.output = {f"new_{name}": True}
            self.text_summary = ""
            self.error = ""

    class _FailResult:
        failed = True
        output = {}
        text_summary = ""
        error = "writer boom"

    async def _fake_execute(task):
        if task.name == "writer":
            return _FailResult()
        return _OkResult(task.name)

    fake_agent = MagicMock()
    fake_agent.execute = AsyncMock(side_effect=_fake_execute)
    handler.router.resolve = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(handler, "_save_rewrite_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(handler, "_summarize_qa_history", lambda: "")

    result = await handler._do_rewrite(pipeline, brief, revision_prompt="tighten")

    assert result is None
    assert experiment_task.status == TaskStatus.COMPLETED
    assert experiment_task.outputs == {"metrics": {"acc": 0.9}}
    assert theory_task.status == TaskStatus.COMPLETED
    assert theory_task.outputs == {"proof": "original"}
