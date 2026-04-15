"""Unit tests for enhanced PaperQAAgent with multi-turn and tool support."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from eurekaclaw.agents.paper_qa.agent import PaperQAAgent
from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.tools.registry import ToolRegistry
from eurekaclaw.skills.injector import SkillInjector
from eurekaclaw.skills.registry import SkillRegistry
from eurekaclaw.memory.manager import MemoryManager


@pytest.fixture
def qa_setup(bus, session_id):
    """Set up PaperQAAgent with mocked LLM client."""
    bus.put("paper_qa_latex", r"\section{Intro}\nSome content.")
    tool_reg = ToolRegistry()
    skill_reg = SkillRegistry()
    injector = SkillInjector(skill_reg)
    memory = MemoryManager(session_id=session_id)

    mock_client = MagicMock()
    agent = PaperQAAgent(
        bus=bus,
        tool_registry=tool_reg,
        skill_injector=injector,
        memory=memory,
        client=mock_client,
    )
    return agent, mock_client


def _make_text_response(text: str):
    """Create a mock Anthropic response with a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    return response


def test_get_tool_names_includes_external_tools(qa_setup):
    agent, _ = qa_setup
    names = agent.get_tool_names()
    assert "arxiv_search" in names
    assert "semantic_scholar" in names
    assert "web_search" in names
    assert "latex_section_read" in names


@pytest.mark.asyncio
async def test_ask_returns_answer(qa_setup):
    agent, mock_client = qa_setup
    mock_client.messages.create = AsyncMock(
        return_value=_make_text_response("The bound follows from Jensen's inequality.")
    )

    result = await agent.ask(
        question="Why is the bound O(n^2)?",
        latex=r"\section{Intro}\nContent here.",
        history=[],
    )
    assert result.success
    assert "Jensen" in result.output["answer"]


@pytest.mark.asyncio
async def test_ask_passes_history(qa_setup):
    agent, mock_client = qa_setup
    mock_client.messages.create = AsyncMock(
        return_value=_make_text_response("Yes, regularity helps.")
    )

    history = [
        {"role": "user", "content": "Is the bound tight?"},
        {"role": "assistant", "content": "Not for regular graphs."},
    ]
    result = await agent.ask(
        question="Can we use regularity?",
        latex=r"\section{Intro}\nContent.",
        history=history,
    )
    assert result.success

    # Verify the messages sent to the LLM include history + new question
    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
    assert len(messages) == 3  # 2 history + 1 new question
    assert messages[0]["content"] == "Is the bound tight?"
    assert messages[2]["content"] == "Can we use regularity?"


@pytest.mark.asyncio
async def test_ask_empty_latex_fails(qa_setup):
    agent, _ = qa_setup
    result = await agent.ask(question="Hello?", latex="", history=[])
    assert result.failed
    assert "LaTeX" in result.error


@pytest.mark.asyncio
async def test_ask_empty_question_fails(qa_setup):
    agent, _ = qa_setup
    result = await agent.ask(question="", latex="some latex", history=[])
    assert result.failed
    assert "question" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_backward_compat(qa_setup):
    """execute() should delegate to ask() for backward compatibility."""
    agent, mock_client = qa_setup
    mock_client.messages.create = AsyncMock(
        return_value=_make_text_response("Answer from execute path.")
    )

    from eurekaclaw.types.tasks import Task
    task = Task(
        task_id="t1",
        name="paper_qa",
        agent_role="writer",
        description="What is the main result?",
    )
    result = await agent.execute(task)
    assert result.success
    assert "execute path" in result.output["answer"]
