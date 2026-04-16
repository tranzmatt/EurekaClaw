from __future__ import annotations

from eurekaclaw.agents.session import AgentSession
from eurekaclaw.agents.theory.checkpoint import ProofCheckpoint
from eurekaclaw.types.artifacts import FailedAttempt, ProofRecord, TheoryState


def test_agent_session_compaction_preserves_recent_tail() -> None:
    session = AgentSession()
    session.add_user("Original task: prove something hard.")
    session.add_assistant("First attempt and intermediate result.")
    session.add_user("Tool result: lemma search output.")
    session.add_assistant("Second attempt with refined plan.")
    session.add_user("Latest counterexample observation.")

    record = session.compress_to_summary(
        "Original task: prove something hard.",
        "- Found two useful lemmas\n- Need to repair the final step",
        preserve_recent_messages=2,
        reason="proactive",
    )

    messages = session.get_messages()
    assert len(messages) == 3
    assert "Context Compaction Boundary" in messages[0]["content"]
    assert "Need to repair the final step" in messages[0]["content"]
    assert messages[1]["content"] == "Second attempt with refined plan."
    assert messages[2]["content"] == "Latest counterexample observation."
    assert record.reason == "proactive"
    assert record.original_message_count == 5
    assert record.preserved_message_count == 2
    assert session.latest_compaction() is not None


def test_proof_checkpoint_persists_compact_context_summary(
    tmp_path,
    monkeypatch,
) -> None:
    from eurekaclaw.config import settings

    monkeypatch.setattr(settings, "eurekaclaw_dir", tmp_path)

    state = TheoryState(
        session_id="sess-1",
        theorem_id="thm-1",
        informal_statement="Informal theorem statement",
        formal_statement="Formal theorem statement",
        open_goals=["lemma_c"],
        status="in_progress",
    )
    state.proven_lemmas["lemma_a"] = ProofRecord(
        lemma_id="lemma_a",
        proof_text="proof a",
        verified=True,
    )
    state.proven_lemmas["lemma_b"] = ProofRecord(
        lemma_id="lemma_b",
        proof_text="proof b",
        verified=True,
    )
    state.failed_attempts.append(
        FailedAttempt(
            lemma_id="lemma_c",
            attempt_text="bad proof",
            failure_reason="timeout after long context build-up",
            iteration=2,
        )
    )

    cp = ProofCheckpoint("sess-1")
    cp.save(
        state,
        next_stage="lemma_developer",
        outer_iter=3,
        current_spec=[{"name": "lemma_developer"}],
        original_spec=[{"name": "paper_reader"}, {"name": "lemma_developer"}],
        domain="machine learning theory",
        research_brief_json="{}",
    )

    loaded_state, meta = cp.load()
    assert loaded_state.session_id == "sess-1"
    assert meta["next_stage"] == "lemma_developer"
    assert "Checkpoint resume summary:" in meta["context_summary"]
    assert "proven_lemmas: 2" in meta["context_summary"]
    assert "timeout after long context build-up" in meta["context_summary"]
