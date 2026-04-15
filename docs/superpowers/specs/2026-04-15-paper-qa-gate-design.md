# Paper QA Gate — Design Spec

## Overview

Enhance the post-writer Paper QA Gate to support interactive review, multi-turn Q&A with tool-equipped agent, and unlimited rewrite cycles. Terminal (CLI) implementation first; UI will follow in a separate phase.

## Problem

The current `_handle_paper_qa_gate()` in `meta_orchestrator.py`:
- Skips entirely in CLI mode (`if not EUREKACLAW_UI_MODE: return`)
- PaperQAAgent is single-shot, no tools, no conversation history
- Rewrite path has no error recovery — failure crashes the session
- QA history is not persisted — lost on interruption
- All logic is hardcoded inside MetaOrchestrator, making it hard to test or extend

## Goals

1. CLI users can review the paper after writer completes (y/N prompt, default skip)
2. Multi-turn QA: user asks questions, QA Agent answers with tool access, user can keep asking
3. Rewrite loop: user can trigger theory + writer re-run with feedback, then review again
4. No limit on rewrite cycles — user controls when to stop
5. Graceful failure recovery: rewrite failure rolls back to previous paper version
6. QA history persisted to JSONL for durability and rewrite context
7. Paper versions saved (paper_v1.tex, paper_v2.tex, ...) for comparison
8. Modular: gate logic extracted from MetaOrchestrator into dedicated handler

## Non-Goals

- UI/frontend changes (separate phase)
- Hook-based gate registry refactor (future PR)
- PDF compilation in CLI mode (LaTeX source is sufficient)

## Architecture

### New Files

#### 1. `eurekaclaw/orchestrator/paper_qa_handler.py`

Central controller for the Paper QA Gate flow.

```
class PaperQAHandler:
    """Encapsulates the full Paper QA Gate flow for CLI (and later UI)."""
```

**Constructor dependencies:**
- `bus: KnowledgeBus` — read LaTeX, store QA answers
- `agents: dict[AgentRole, BaseAgent]` — access theory/writer for rewrite
- `router: TaskRouter` — resolve agent for task execution
- `client: LLMClient` — passed to QA agent
- `tool_registry: ToolRegistry` — passed to QA agent
- `skill_injector: SkillInjector` — passed to QA agent
- `memory: MemoryManager` — passed to QA agent
- `gate_controller: GateController` — for stage summaries

**Internal state:**
- `_qa_agent: PaperQAAgent | None` — lazy-created on first question
- `_history: list[dict]` — QA conversation turns `[{"role": "user"|"assistant", "content": ...}]`
- `_paper_version: int` — counter starting at 1
- `_session_dir: Path` — `~/.eurekaclaw/runs/{session_id}/`

**Public API:**

```python
async def run(self, pipeline: TaskPipeline, brief: ResearchBrief) -> None
```

**Flow:**

1. Extract `latex_paper` from writer task outputs
2. Save `paper_v1.tex` to session dir
3. Prompt: `"Review the paper? [y/N]"` — default N skips
4. If yes, enter outer review loop

**Outer loop (review → rewrite cycle):**

```
while True:
    display_latex_preview(latex)   # first 80 + last 20 lines
    action = await qa_loop(latex)  # inner QA loop
    if action == "accept":
        break
    elif action == "rewrite":
        new_latex = await do_rewrite(pipeline, brief)
        if new_latex is None:
            latex = rollback_paper()   # recovery: load previous version
        else:
            latex = new_latex
            save_paper_version(latex)  # paper_v{n+1}.tex
```

**Inner loop (QA multi-turn):**

```
while True:
    question = prompt_question()    # Enter = accept
    if not question:
        return "accept"
    answer = await ask_qa_agent(latex, question)
    append to history + persist JSONL
    choice = prompt_after_answer()  # [a]ccept / [q]uestion / [r]ewrite
    if choice == "a": return "accept"
    if choice == "r": return "rewrite"
    # choice == "q": continue inner loop
```

**Rewrite flow (`do_rewrite`):**

1. Prompt user for revision instructions
2. Build rewrite context: QA history summary + revision prompt
3. Reset theory task: inject feedback into description, set status=PENDING, retries=0
4. Reset writer task: status=PENDING, retries=0
5. Execute theory agent, then writer agent (sequential)
6. On success: return new LaTeX
7. On failure: log error, return None (triggers rollback)
8. Re-arm paper_qa gate via `review_gate.reset_paper_qa()`

**Persistence:**

- `paper_qa_history.jsonl`: append-only, one JSON object per line
  ```json
  {"role": "user", "content": "...", "ts": "2026-04-15T10:30:00Z", "version": 1}
  {"role": "assistant", "content": "...", "ts": "2026-04-15T10:30:05Z", "version": 1}
  ```
- `paper_v{n}.tex`: full LaTeX source per version
- `rewrite_context_v{n}.json`: QA summary + revision prompt used for each rewrite

**LaTeX preview display:**

- Show first 80 lines + last 20 lines via Rich Panel
- Print total line count
- Truncation marker: `... ({remaining} lines omitted) ...`

#### 2. `eurekaclaw/tools/latex_section.py`

New tool allowing QA Agent to read specific paper sections without full-source context.

```
class LatexSectionReadTool(BaseTool):
    name = "latex_section_read"
    description = "Read a specific section from the paper LaTeX source by name or number."
```

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "section": {
      "type": "string",
      "description": "Section name (e.g. 'Introduction') or number (e.g. '3.1')"
    }
  },
  "required": ["section"]
}
```

**Implementation:**
- Read `paper_qa_latex` from bus
- Parse `\section{}`, `\subsection{}`, `\subsubsection{}` hierarchy via regex
- Match query against section titles (case-insensitive, fuzzy) and numbers
- Return the matched section content (from header to next same-or-higher-level header)
- If no match: return list of available section names for the agent to retry

**Constructor:** Takes `bus: KnowledgeBus` to access paper LaTeX.

### Modified Files

#### 3. `eurekaclaw/agents/paper_qa/agent.py`

Transform from single-shot to multi-turn, tool-equipped, streaming agent.

**Changes:**
- `get_tool_names()` returns `["arxiv_search", "semantic_scholar", "web_search", "latex_section_read"]`
- New `ask()` method for handler-driven calls:
  ```python
  async def ask(self, question: str, latex: str, history: list[dict]) -> AgentResult
  ```
- LaTeX placed in system prompt (not user message) to enable Anthropic prompt cache across turns
- `_streaming_tool_loop()`: stream response to console, handle tool_use blocks, max 5 tool iterations
- `execute()` preserved for backward compatibility — delegates to `ask()` with empty history

**System prompt structure:**
```
Role instructions + tool usage guidance
---
PAPER (LaTeX source):
```latex
{full latex}
```
```

**Message structure:**
```
[...prior QA turns from history...]
{"role": "user", "content": "{current question}"}
```

#### 4. `eurekaclaw/orchestrator/meta_orchestrator.py`

Replace `_handle_paper_qa_gate` body:

```python
async def _handle_paper_qa_gate(self, pipeline, brief):
    from eurekaclaw.orchestrator.paper_qa_handler import PaperQAHandler
    handler = PaperQAHandler(
        bus=self.bus, agents=self.agents, router=self.router,
        client=self.client, tool_registry=self.tool_registry,
        skill_injector=self.skill_injector, memory=self.memory,
        gate_controller=self.gate,
    )
    await handler.run(pipeline, brief)
```

Remove the UI-only guard (`if not EUREKACLAW_UI_MODE: return`).

#### 5. `eurekaclaw/tools/registry.py`

In `build_default_registry()`, add:

```python
from eurekaclaw.tools.latex_section import LatexSectionReadTool
registry.register(LatexSectionReadTool(bus=bus))
```

This requires `build_default_registry` to accept an optional `bus` parameter, or the tool is registered later by MetaOrchestrator after bus creation.

**Decision:** Register `latex_section_read` in MetaOrchestrator.__init__ after bus is available, alongside the existing tool registry setup. This avoids changing the `build_default_registry` signature.

#### 6. `eurekaclaw/ui/review_gate.py`

Add `reset_paper_qa()` (mirrors existing `reset_theory()`):

```python
def reset_paper_qa(session_id: str) -> None:
    """Re-arm the paper QA gate for another review round after rewrite."""
    with _lock:
        if session_id in _paper_qa:
            _paper_qa[session_id] = _GateEntry()
```

## Data Flow

```
Writer completes
    │
    ▼
PaperQAHandler.run()
    │
    ├─ reads latex from writer task outputs
    ├─ saves paper_v1.tex
    ├─ CLI prompt: "Review the paper? [y/N]"
    │
    ▼ (user says y)
Outer Loop
    │
    ├─ display_latex_preview()
    │
    ▼
Inner QA Loop
    │
    ├─ user types question
    ├─ PaperQAAgent.ask(question, latex, history)
    │     ├─ system prompt = instructions + full LaTeX (cached)
    │     ├─ messages = history + question
    │     ├─ streaming response with tool calls:
    │     │     arxiv_search, semantic_scholar, web_search, latex_section_read
    │     └─ returns answer
    ├─ append to history + persist JSONL
    ├─ prompt: [a]ccept / [q]uestion / [r]ewrite
    │     ├─ "a" → break both loops
    │     ├─ "q" → continue inner loop
    │     └─ "r" → break inner, enter rewrite
    │
    ▼ (rewrite chosen)
do_rewrite()
    │
    ├─ prompt user for revision instructions
    ├─ build context: QA summary + revision prompt
    ├─ inject feedback into theory task description
    ├─ re-run theory agent
    │     ├─ success → re-run writer agent
    │     │     ├─ success → save paper_v{n+1}.tex, continue outer loop
    │     │     └─ failure → rollback to paper_v{n}.tex
    │     └─ failure (partial) → writer generates paper with [TODO] markers
    │           └─ save, continue outer loop (user can QA the TODOs)
    │
    ▼
Pipeline completes → post-run learning
```

## Error Recovery

| Failure | Strategy |
|---------|----------|
| Theory agent fails completely | Rollback to `paper_v{n-1}.tex`, notify user, return to gate |
| Theory agent partial (some lemmas fail) | Writer generates paper with `[TODO]` markers for failed lemmas, user can QA/rewrite again |
| Writer agent fails | Rollback to `paper_v{n-1}.tex`, notify user |
| QA Agent fails (single question) | Print error, let user ask another question or accept |
| Session interrupted during QA | History persisted to JSONL, paper versions on disk — no data loss |
| Keyboard interrupt (Ctrl+C) | Catch gracefully, treat as "accept" for current loop level |

## CLI Interaction Transcript

```
✓ Done: writer
📄 Session Summary
  Domain:  spectral_graph_theory
  Proof:   proved — 5 lemmas
  Experiment: alignment=0.92

Review the paper? [y/N]: y

── Paper Preview (v1) ─────────────────────────────────────────
  1 │ \documentclass{article}
  2 │ \title{On the Convergence of Spectral Methods}
  3 │ \author{EurekaClaw}
    │ ...
 80 │ \subsection{Proof of Theorem 2}
    │
    │ ... (262 lines omitted) ...
    │
323 │ \bibliography{refs}
342 │ \end{document}
────────────────────────────────────────────────────────────────
342 lines total

Question (Enter to accept): The bound in Theorem 2 seems loose — is O(n^2) tight?

⏳ Searching references...
  ✓ arxiv_search: "spectral gap tight bound"
  ✓ latex_section_read: section "Theorem 2"

The bound O(n^2) follows from applying Weyl's inequality to the
Laplacian eigenvalues. However, [Chen et al. 2024] showed that for
regular graphs, this can be tightened to O(n log n) using interlacing
arguments. The current proof does not exploit regularity.

What next?  [a]ccept  [q]uestion  [r]ewrite: q

Question (Enter to accept): Can we tighten it using the regularity assumption from Section 2?

⏳ Analyzing...
  ✓ latex_section_read: section "2"

Yes — Section 2 establishes k-regularity (Lemma 2.1). Combining with
the interlacing technique from [Chen et al. 2024, Thm 3.2], the bound
in Theorem 2 can be improved to O(n log n). The key change would be
in the proof of Lemma 4, replacing Weyl's inequality with Cauchy
interlacing for regular graphs.

What next?  [a]ccept  [q]uestion  [r]ewrite: r

Describe what to fix:
→ Tighten the bound in Theorem 2 from O(n^2) to O(n log n) using
  Cauchy interlacing for k-regular graphs per Lemma 2.1

Re-running theory + writer with feedback...
▶ Running: theory
✓ Done: theory (3 lemmas updated)
▶ Running: writer
✓ Done: writer

── Paper Preview (v2) ─────────────────────────────────────────
  1 │ \documentclass{article}
    │ ...
────────────────────────────────────────────────────────────────
351 lines total

Question (Enter to accept): ↵

✓ Paper accepted (v2)
```

## Testing Strategy

- Unit tests for `LatexSectionReadTool._extract_section()` with various LaTeX structures
- Unit tests for `PaperQAHandler` flow logic with mocked agents (skip/accept/rewrite paths)
- Unit test for history JSONL persistence and paper version saving
- Integration test: full CLI flow with mocked LLM responses (skip path, single QA, rewrite path)
