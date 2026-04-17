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
