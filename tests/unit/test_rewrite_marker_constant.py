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
