"""Unit tests for SessionLoader — reconstruct session from disk."""

import json
import pytest
from pathlib import Path

from eurekaclaw.orchestrator.session_loader import SessionLoader


@pytest.fixture
def mock_session_dir(tmp_path, monkeypatch):
    """Create a fake session directory with persisted artifacts."""
    from eurekaclaw.config import settings
    monkeypatch.setattr(settings, "runs_dir", tmp_path)

    session_id = "test-session-001"
    session_dir = tmp_path / session_id
    session_dir.mkdir()

    brief = {
        "session_id": session_id,
        "input_mode": "detailed",
        "domain": "spectral_graph",
        "query": "Convergence of spectral methods",
    }
    (session_dir / "research_brief.json").write_text(json.dumps(brief))

    pipeline = {
        "pipeline_id": "p1",
        "session_id": session_id,
        "tasks": [
            {
                "task_id": "w1",
                "name": "writer",
                "agent_role": "writer",
                "status": "completed",
                "outputs": {"latex_paper": "\\section{Intro}\nTest paper."},
            },
            {
                "task_id": "g1",
                "name": "paper_qa_gate",
                "agent_role": "orchestrator",
                "status": "completed",
            },
        ],
    }
    (session_dir / "pipeline.json").write_text(json.dumps(pipeline))

    return session_id, session_dir


def test_load_session_returns_bus_brief_pipeline(mock_session_dir):
    session_id, _ = mock_session_dir
    bus, brief, pipeline = SessionLoader.load(session_id)
    assert bus.session_id == session_id
    assert brief.domain == "spectral_graph"
    assert pipeline is not None
    assert any(t.name == "writer" for t in pipeline.tasks)


def test_load_session_puts_latex_on_bus(mock_session_dir):
    session_id, _ = mock_session_dir
    bus, _, _ = SessionLoader.load(session_id)
    assert "Test paper" in (bus.get("paper_qa_latex") or "")


def test_load_session_not_found(tmp_path, monkeypatch):
    from eurekaclaw.config import settings
    monkeypatch.setattr(settings, "runs_dir", tmp_path)
    with pytest.raises(FileNotFoundError):
        SessionLoader.load("nonexistent-session")


def test_load_session_no_latex_raises(tmp_path, monkeypatch):
    from eurekaclaw.config import settings
    monkeypatch.setattr(settings, "runs_dir", tmp_path)

    session_id = "no-paper-session"
    session_dir = tmp_path / session_id
    session_dir.mkdir()

    brief = {"session_id": session_id, "input_mode": "detailed", "domain": "test", "query": "test"}
    (session_dir / "research_brief.json").write_text(json.dumps(brief))

    pipeline = {
        "pipeline_id": "p1",
        "session_id": session_id,
        "tasks": [{"task_id": "w1", "name": "writer", "agent_role": "writer", "status": "failed", "outputs": {}}],
    }
    (session_dir / "pipeline.json").write_text(json.dumps(pipeline))

    with pytest.raises(ValueError, match="No paper"):
        SessionLoader.load(session_id)


def test_load_session_partial_id(mock_session_dir, tmp_path, monkeypatch):
    from eurekaclaw.config import settings
    monkeypatch.setattr(settings, "runs_dir", tmp_path)
    session_id, _ = mock_session_dir
    prefix = session_id[:12]
    bus, brief, _ = SessionLoader.load(prefix)
    assert bus.session_id == session_id
