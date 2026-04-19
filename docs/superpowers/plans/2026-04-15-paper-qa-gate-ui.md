# Paper QA Gate UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the modal PaperQAGate with a full-panel Paper Review experience featuring resizable PDF/LaTeX split view and streaming QA chat.

**Architecture:** New `PaperReviewPanel` component conditionally replaces the workspace tabs when `paper_qa_gate` status is `awaiting_gate`. The panel is composed of `PaperViewer` (left), `ResizableDivider`, and `QAChat` (right). Two new backend endpoints serve QA ask/history. The existing gate submission endpoint is preserved for accept/rewrite actions.

**Tech Stack:** React 18, TypeScript, Vite, CSS custom properties (existing theme), existing `apiGet`/`apiPost` helpers

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx` | Create | Main split-view container with resizable divider |
| `frontend/src/components/workspace/paper-review/PaperViewer.tsx` | Create | Left panel: PDF/LaTeX tabs, download buttons, version bar |
| `frontend/src/components/workspace/paper-review/QAChat.tsx` | Create | Right panel: messages, input, accept/rewrite buttons |
| `frontend/src/components/workspace/paper-review/ChatMessage.tsx` | Create | Individual message rendering (user/agent/system) |
| `frontend/src/components/workspace/paper-review/ToolSteps.tsx` | Create | Collapsed tool-use badges with status dots |
| `frontend/src/components/workspace/paper-review/RewriteOverlay.tsx` | Create | Progress overlay during theory+writer re-run |
| `frontend/src/styles/paper-review.css` | Create | All paper review styles |
| `frontend/src/types/run.ts` | Modify | Add QA message types |
| `frontend/src/components/workspace/WorkspaceTabs.tsx` | Modify | Conditionally render PaperReviewPanel |
| `frontend/src/components/workspace/GateOverlay.tsx` | Modify | Remove PaperQAGate case |
| `frontend/src/hooks/usePolling.ts` | Modify | Auto-switch when paper_qa_gate activates |
| `eurekaclaw/ui/server.py` | Modify | Add `/paper-qa/ask` and `/paper-qa/history` endpoints |

---

### Task 1: Add QA Message Types

**Files:**
- Modify: `frontend/src/types/run.ts:184-185`

- [ ] **Step 1: Add QAMessage and ToolStep types to run.ts**

In `frontend/src/types/run.ts`, add after the `Artifacts` interface (after line 185):

```typescript
// ── Paper QA ─────────────────────────────────────────────────────────────────

export interface ToolStep {
  tool: string;
  input: string;
  status: 'pending' | 'running' | 'done' | 'failed';
}

export interface QAMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  ts?: string;
  version?: number;
  tool_steps?: ToolStep[];
}
```

Also update the `Artifacts` interface to include QA history. Change line 184:

```typescript
  paper_qa_answer?: string | null;
```

to:

```typescript
  paper_qa_answer?: string | null;
  paper_qa_history?: QAMessage[];
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /Users/chenggong/Downloads/EurekaClaw/frontend && npx tsc --noEmit`
Expected: No new errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/run.ts
git commit -m "feat(ui): add QAMessage and ToolStep types for paper review"
```

---

### Task 2: Backend — QA Ask and History Endpoints

**Files:**
- Modify: `eurekaclaw/ui/server.py`

- [ ] **Step 1: Add paper-qa/ask and paper-qa/history POST/GET handlers**

In `eurekaclaw/ui/server.py`, find the `do_POST` method. Before the gate submission section (line 1808: `# Gate submission endpoints`), add:

```python
        # ── Paper QA endpoints ────────────────────────────────────────────
        parts = parsed.path.strip("/").split("/")

        # POST /api/runs/<run_id>/paper-qa/ask
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "runs"
                and parts[3] == "paper-qa"):
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if (len(parts) == 5 and parts[0] == "api" and parts[1] == "runs"
                and parts[3] == "paper-qa" and parts[4] == "ask"):
            run_id = parts[2]
            run = self.state.get_run(run_id)
            if run is None:
                self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
                return
            session_id = run.eureka_session_id
            if not session_id:
                self._send_json({"error": "No active session"}, status=HTTPStatus.BAD_REQUEST)
                return
            payload = self._read_json()
            question = str(payload.get("question", "")).strip()
            history = payload.get("history", [])
            if not question:
                self._send_json({"error": "No question provided"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Get LaTeX from bus
            bus = run.bus if hasattr(run, "bus") else None
            if bus is None:
                from eurekaclaw.ui.server import _active_sessions
                session = _active_sessions.get(session_id)
                bus = session.bus if session else None
            latex = bus.get("paper_qa_latex") or "" if bus else ""

            # Create and call PaperQAAgent
            import asyncio
            from eurekaclaw.agents.paper_qa.agent import PaperQAAgent
            from eurekaclaw.tools.registry import build_default_registry
            from eurekaclaw.skills.injector import SkillInjector
            from eurekaclaw.skills.registry import SkillRegistry
            from eurekaclaw.memory.manager import MemoryManager
            from eurekaclaw.llm import create_client

            tool_registry = build_default_registry(bus=bus) if bus else build_default_registry()
            agent = PaperQAAgent(
                bus=bus,
                tool_registry=tool_registry,
                skill_injector=SkillInjector(SkillRegistry()),
                memory=MemoryManager(session_id=session_id),
                client=create_client(),
            )
            clean_history = [
                {"role": h.get("role", "user"), "content": h.get("content", "")}
                for h in history
            ]

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    agent.ask(question=question, latex=latex, history=clean_history)
                )
                loop.close()
            except Exception as e:
                self._send_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if result.failed:
                self._send_json({"error": result.error}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            # Persist to history JSONL
            import json as _json
            from datetime import datetime, timezone
            from pathlib import Path as _Path
            from eurekaclaw.config import settings as _settings
            history_dir = _settings.runs_dir / session_id
            history_dir.mkdir(parents=True, exist_ok=True)
            history_file = history_dir / "paper_qa_history.jsonl"
            ts = datetime.now(timezone.utc).isoformat()
            with history_file.open("a", encoding="utf-8") as f:
                f.write(_json.dumps({"role": "user", "content": question, "ts": ts}, ensure_ascii=False) + "\n")
                f.write(_json.dumps({"role": "assistant", "content": result.output.get("answer", ""), "ts": ts}, ensure_ascii=False) + "\n")

            self._send_json({
                "answer": result.output.get("answer", ""),
                "tool_steps": [],  # TODO: capture from agent in future iteration
            })
            return
```

In the `do_GET` method, add before the final 404 handler:

```python
        # GET /api/runs/<run_id>/paper-qa/history
        parts = parsed.path.strip("/").split("/")
        if (len(parts) == 5 and parts[0] == "api" and parts[1] == "runs"
                and parts[3] == "paper-qa" and parts[4] == "history"):
            run_id = parts[2]
            run = self.state.get_run(run_id)
            if run is None:
                self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
                return
            session_id = run.eureka_session_id or ""
            import json as _json
            from eurekaclaw.config import settings as _settings
            history_file = _settings.runs_dir / session_id / "paper_qa_history.jsonl"
            messages = []
            if history_file.exists():
                for line in history_file.read_text(encoding="utf-8").strip().split("\n"):
                    if line.strip():
                        try:
                            messages.append(_json.loads(line))
                        except _json.JSONDecodeError:
                            pass
            self._send_json({"messages": messages})
            return
```

- [ ] **Step 2: Verify server starts without import errors**

Run: `cd /Users/chenggong/Downloads/EurekaClaw && source .venv/bin/activate && python -c "from eurekaclaw.ui.server import EurekaRequestHandler; print('Server imports OK')"`
Expected: `Server imports OK`

- [ ] **Step 3: Commit**

```bash
git add eurekaclaw/ui/server.py
git commit -m "feat(api): add paper-qa/ask and paper-qa/history endpoints"
```

---

### Task 3: CSS Styles for Paper Review

**Files:**
- Create: `frontend/src/styles/paper-review.css`

- [ ] **Step 1: Create paper-review.css with all component styles**

```css
/* ── Paper Review Panel ─────────────────────────────────────────────────── */

.paper-review-panel {
  display: flex;
  height: 100%;
  min-height: 0;
  background: var(--surface);
}

/* ── Paper Viewer (left) ──────────────────────────────────────────────── */

.paper-viewer {
  display: flex;
  flex-direction: column;
  background: var(--surface-strong);
  min-width: 0;
  overflow: hidden;
}

.pv-tab-bar {
  display: flex;
  align-items: center;
  padding: 10px 16px 0;
  border-bottom: 1.5px solid var(--line);
  gap: 0;
  flex-shrink: 0;
}

.pv-tab {
  padding: 8px 18px;
  font-size: 0.82rem;
  font-weight: 500;
  color: var(--muted);
  background: none;
  border: none;
  cursor: pointer;
  margin-bottom: -1.5px;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
}

.pv-tab.is-active {
  font-weight: 600;
  color: var(--primary);
  border-bottom-color: var(--primary);
}

.pv-tab:hover:not(.is-active) {
  color: var(--text);
}

.pv-tab-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}

.pv-download-btn {
  padding: 5px 12px;
  font-size: 0.72rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--muted);
  background: none;
  cursor: pointer;
  text-decoration: none;
  transition: border-color 0.15s, color 0.15s;
}

.pv-download-btn:hover {
  border-color: var(--primary-soft);
  color: var(--primary);
}

.pv-content {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.pv-pdf-frame {
  width: 100%;
  height: 100%;
  border: none;
}

.pv-latex-source {
  padding: 16px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.78rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text);
}

.pv-version-bar {
  padding: 8px 16px;
  border-top: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  background: var(--surface-strong);
}

.pv-version-label {
  font-size: 0.7rem;
  color: var(--muted);
}

.pv-version-sep {
  font-size: 0.65rem;
  color: var(--dim);
}

.pv-compiled-badge {
  font-size: 0.65rem;
  color: var(--ok);
  background: #e8f0eb;
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: auto;
}

/* ── Resizable Divider ────────────────────────────────────────────────── */

.review-divider {
  width: 5px;
  background: var(--line);
  cursor: col-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}

.review-divider:hover,
.review-divider.is-dragging {
  background: var(--primary-soft);
}

.review-divider-handle {
  width: 1.5px;
  height: 32px;
  background: var(--muted);
  border-radius: 1px;
  opacity: 0.4;
}

/* ── QA Chat (right) ──────────────────────────────────────────────────── */

.qa-chat {
  display: flex;
  flex-direction: column;
  background: #faf8f5;
  min-width: 0;
  overflow: hidden;
}

.qa-chat-header {
  padding: 12px 16px;
  border-bottom: 1.5px solid var(--line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.qa-chat-title {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text);
}

.qa-chat-badge {
  font-size: 0.68rem;
  color: var(--muted);
  background: var(--surface-cool);
  padding: 3px 10px;
  border-radius: 8px;
  margin-left: auto;
}

.qa-chat-badge--rewriting {
  color: var(--warn);
  background: #fdf3e4;
}

.qa-messages {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
}

/* ── Chat Messages ────────────────────────────────────────────────────── */

.qa-msg-wrap {
  display: flex;
  flex-direction: column;
}

.qa-msg-wrap--user {
  align-items: flex-end;
}

.qa-msg-wrap--agent {
  align-items: flex-start;
}

.qa-msg-wrap--system {
  align-items: center;
}

.qa-msg-user {
  background: var(--primary);
  color: #fff;
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  max-width: 85%;
  font-size: 0.82rem;
  line-height: 1.5;
}

.qa-msg-agent {
  background: var(--surface-strong);
  border: 1px solid var(--line);
  padding: 10px 14px;
  border-radius: 14px 14px 14px 4px;
  max-width: 85%;
  font-size: 0.82rem;
  line-height: 1.55;
  color: var(--text);
}

.qa-msg-system {
  font-size: 0.7rem;
  color: var(--warn);
  background: #fdf3e4;
  border: 1px solid #f0dfc0;
  padding: 4px 12px;
  border-radius: 10px;
}

.qa-msg-ts {
  font-size: 0.62rem;
  color: var(--dim);
  margin-top: 4px;
}

/* ── Tool Steps ───────────────────────────────────────────────────────── */

.tool-steps {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 6px;
}

.tool-step {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--surface-cool);
  border-radius: 8px;
  width: fit-content;
  font-size: 0.68rem;
  color: var(--muted);
}

.tool-step-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tool-step-dot--done {
  background: var(--ok);
}

.tool-step-dot--running {
  background: var(--primary);
  animation: qa-pulse 1.2s infinite;
}

.tool-step-dot--pending {
  background: var(--line);
}

@keyframes qa-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

/* ── Chat Input ───────────────────────────────────────────────────────── */

.qa-input-area {
  padding: 12px 16px;
  border-top: 1.5px solid var(--line);
  background: var(--surface-strong);
  flex-shrink: 0;
}

.qa-input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.qa-input-field {
  flex: 1;
  background: var(--surface-cool);
  border: 1.5px solid var(--line);
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 0.82rem;
  font-family: inherit;
  color: var(--text);
  resize: none;
  min-height: 20px;
  max-height: 120px;
  transition: border-color 0.15s;
}

.qa-input-field:focus {
  outline: none;
  border-color: var(--primary-soft);
}

.qa-input-field::placeholder {
  color: var(--dim);
}

.qa-send-btn {
  padding: 8px 12px;
  background: var(--primary);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-size: 0.78rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s;
}

.qa-send-btn:hover {
  background: var(--primary-strong);
}

.qa-send-btn:disabled {
  background: var(--muted);
  cursor: not-allowed;
  opacity: 0.5;
}

.qa-action-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.qa-accept-btn {
  padding: 6px 14px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  font-size: 0.72rem;
  color: var(--muted);
  background: none;
  cursor: pointer;
  font-weight: 500;
  transition: border-color 0.15s, color 0.15s;
}

.qa-accept-btn:hover {
  border-color: var(--ok);
  color: var(--ok);
}

.qa-rewrite-btn {
  padding: 6px 14px;
  border: 1.5px solid var(--primary-soft);
  border-radius: 10px;
  font-size: 0.72rem;
  color: var(--primary);
  background: #f0f6fd;
  cursor: pointer;
  font-weight: 500;
  transition: background 0.15s;
}

.qa-rewrite-btn:hover {
  background: var(--primary-soft);
}

.qa-input-area--disabled {
  opacity: 0.5;
  pointer-events: none;
}

/* ── Rewrite Overlay ──────────────────────────────────────────────────── */

.rewrite-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 253, 249, 0.85);
  backdrop-filter: blur(2px);
  z-index: 10;
}

.rewrite-overlay-content {
  text-align: center;
  max-width: 320px;
}

.rewrite-spinner {
  width: 48px;
  height: 48px;
  border: 3px solid var(--primary-soft);
  border-top-color: var(--primary);
  border-radius: 50%;
  margin: 0 auto 20px;
  animation: qa-spin 1s linear infinite;
}

@keyframes qa-spin {
  to { transform: rotate(360deg); }
}

.rewrite-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8px;
}

.rewrite-desc {
  font-size: 0.82rem;
  color: var(--muted);
  margin-bottom: 20px;
  line-height: 1.5;
}

.rewrite-steps {
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--surface-cool);
  padding: 16px 20px;
  border-radius: 12px;
  border: 1px solid var(--line);
}

.rewrite-step {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rewrite-step--pending {
  opacity: 0.45;
}

.rewrite-step-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--primary);
  border-top-color: transparent;
  border-radius: 50%;
  flex-shrink: 0;
  animation: qa-spin 0.8s linear infinite;
}

.rewrite-step-dot {
  width: 20px;
  height: 20px;
  border: 2px solid var(--line);
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.rewrite-step-dot-inner {
  width: 6px;
  height: 6px;
  background: var(--line);
  border-radius: 50%;
}

.rewrite-step-done {
  width: 20px;
  height: 20px;
  border: 2px solid var(--ok);
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ok);
  font-size: 0.7rem;
}

.rewrite-step-name {
  font-size: 0.78rem;
  font-weight: 600;
}

.rewrite-step-name--active {
  color: var(--primary);
}

.rewrite-step-detail {
  font-size: 0.68rem;
  color: var(--muted);
}
```

- [ ] **Step 2: Import the new CSS in index.css**

In `frontend/src/styles/index.css`, add at the very end:

```css
@import './paper-review.css';
```

- [ ] **Step 3: Verify styles load without errors**

Run: `cd /Users/chenggong/Downloads/EurekaClaw/frontend && npx vite build 2>&1 | tail -5`
Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles/paper-review.css frontend/src/styles/index.css
git commit -m "feat(ui): add paper review panel CSS styles"
```

---

### Task 4: ToolSteps Component

**Files:**
- Create: `frontend/src/components/workspace/paper-review/ToolSteps.tsx`

- [ ] **Step 1: Create ToolSteps component**

```typescript
import type { ToolStep } from '@/types';

interface ToolStepsProps {
  steps: ToolStep[];
}

export function ToolSteps({ steps }: ToolStepsProps) {
  if (!steps.length) return null;

  return (
    <div className="tool-steps">
      {steps.map((step, i) => (
        <div className="tool-step" key={i}>
          <span className={`tool-step-dot tool-step-dot--${step.status}`} />
          <span>{step.tool}: {step.input}</span>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/ToolSteps.tsx
git commit -m "feat(ui): add ToolSteps component for QA agent tool badges"
```

---

### Task 5: ChatMessage Component

**Files:**
- Create: `frontend/src/components/workspace/paper-review/ChatMessage.tsx`

- [ ] **Step 1: Create ChatMessage component**

```typescript
import type { QAMessage } from '@/types';
import { ToolSteps } from './ToolSteps';

interface ChatMessageProps {
  message: QAMessage;
}

function timeAgo(ts?: string): string {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins === 1) return '1 min ago';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  return hrs === 1 ? '1 hr ago' : `${hrs} hrs ago`;
}

export function ChatMessage({ message }: ChatMessageProps) {
  if (message.role === 'system') {
    return (
      <div className="qa-msg-wrap qa-msg-wrap--system">
        <span className="qa-msg-system">{message.content}</span>
      </div>
    );
  }

  if (message.role === 'user') {
    return (
      <div className="qa-msg-wrap qa-msg-wrap--user">
        <div className="qa-msg-user">{message.content}</div>
        <span className="qa-msg-ts">{timeAgo(message.ts)}</span>
      </div>
    );
  }

  return (
    <div className="qa-msg-wrap qa-msg-wrap--agent">
      {message.tool_steps && <ToolSteps steps={message.tool_steps} />}
      <div className="qa-msg-agent">{message.content}</div>
      <span className="qa-msg-ts">{timeAgo(message.ts)}</span>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/ChatMessage.tsx
git commit -m "feat(ui): add ChatMessage component with user/agent/system variants"
```

---

### Task 6: RewriteOverlay Component

**Files:**
- Create: `frontend/src/components/workspace/paper-review/RewriteOverlay.tsx`

- [ ] **Step 1: Create RewriteOverlay component**

```typescript
interface RewriteOverlayProps {
  theoryStatus: string;
  writerStatus: string;
}

export function RewriteOverlay({ theoryStatus, writerStatus }: RewriteOverlayProps) {
  const theoryDone = theoryStatus === 'completed' || theoryStatus === 'failed';
  const writerActive = theoryDone && (writerStatus === 'in_progress' || writerStatus === 'running');

  return (
    <div className="rewrite-overlay">
      <div className="rewrite-overlay-content">
        <div className="rewrite-spinner" />
        <div className="rewrite-title">Rewriting paper...</div>
        <div className="rewrite-desc">
          Incorporating your feedback into the proof and regenerating the paper.
        </div>
        <div className="rewrite-steps">
          <div className={`rewrite-step${theoryDone ? '' : ''}`}>
            {theoryDone ? (
              <div className="rewrite-step-done">✓</div>
            ) : (
              <div className="rewrite-step-spinner" />
            )}
            <div>
              <div className={`rewrite-step-name${!theoryDone ? ' rewrite-step-name--active' : ''}`}>
                Theory Agent
              </div>
              <div className="rewrite-step-detail">
                {theoryDone ? 'Done' : 'Re-proving with feedback...'}
              </div>
            </div>
          </div>
          <div className={`rewrite-step${!theoryDone ? ' rewrite-step--pending' : ''}`}>
            {writerActive ? (
              <div className="rewrite-step-spinner" />
            ) : writerStatus === 'completed' ? (
              <div className="rewrite-step-done">✓</div>
            ) : (
              <div className="rewrite-step-dot">
                <div className="rewrite-step-dot-inner" />
              </div>
            )}
            <div>
              <div className={`rewrite-step-name${writerActive ? ' rewrite-step-name--active' : ''}`}>
                Writer Agent
              </div>
              <div className="rewrite-step-detail">
                {writerActive ? 'Generating paper...' : writerStatus === 'completed' ? 'Done' : 'Waiting for theory...'}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/RewriteOverlay.tsx
git commit -m "feat(ui): add RewriteOverlay with theory/writer step progress"
```

---

### Task 7: PaperViewer Component

**Files:**
- Create: `frontend/src/components/workspace/paper-review/PaperViewer.tsx`

- [ ] **Step 1: Create PaperViewer component**

```typescript
import { useState, useCallback } from 'react';
import { apiPost } from '@/api/client';
import { RewriteOverlay } from './RewriteOverlay';
import type { SessionRun } from '@/types';

interface PaperViewerProps {
  run: SessionRun;
  paperVersion: number;
  isRewriting: boolean;
  theoryStatus: string;
  writerStatus: string;
}

interface CompileResponse {
  ok?: boolean;
  error?: string;
}

export function PaperViewer({ run, paperVersion, isRewriting, theoryStatus, writerStatus }: PaperViewerProps) {
  const [activeTab, setActiveTab] = useState<'pdf' | 'latex'>('pdf');
  const [compiling, setCompiling] = useState(false);
  const [compileError, setCompileError] = useState('');

  const latexSource = run.result?.latex_paper || '';
  const pdfPath = run.result?.pdf_path;
  const lineCount = latexSource ? latexSource.split('\n').length : 0;

  const compilePdf = useCallback(async () => {
    setCompiling(true);
    setCompileError('');
    try {
      const res = await apiPost<CompileResponse>(`/api/runs/${run.run_id}/compile-pdf`, {});
      if (!res.ok) setCompileError(res.error || 'Compilation failed');
    } catch (e) {
      setCompileError(String(e));
    } finally {
      setCompiling(false);
    }
  }, [run.run_id]);

  return (
    <div className="paper-viewer" style={{ position: 'relative' }}>
      {/* Tab bar */}
      <div className="pv-tab-bar">
        <button
          className={`pv-tab${activeTab === 'pdf' ? ' is-active' : ''}`}
          onClick={() => setActiveTab('pdf')}
        >
          PDF
        </button>
        <button
          className={`pv-tab${activeTab === 'latex' ? ' is-active' : ''}`}
          onClick={() => setActiveTab('latex')}
        >
          LaTeX
        </button>
        <div className="pv-tab-actions">
          <a
            href={`/api/runs/${run.run_id}/artifacts/paper.tex`}
            target="_blank"
            rel="noreferrer"
            className="pv-download-btn"
          >
            ⬇ .tex
          </a>
          <a
            href={`/api/runs/${run.run_id}/artifacts/paper.pdf`}
            target="_blank"
            rel="noreferrer"
            className="pv-download-btn"
          >
            ⬇ .pdf
          </a>
        </div>
      </div>

      {/* Content */}
      <div className="pv-content">
        {activeTab === 'pdf' ? (
          pdfPath ? (
            <iframe
              className="pv-pdf-frame"
              src={`/api/runs/${run.run_id}/artifacts/paper.pdf`}
              title="Paper PDF"
            />
          ) : (
            <div style={{ padding: '2rem', textAlign: 'center' }}>
              <p style={{ color: 'var(--muted)', marginBottom: '1rem' }}>
                {compileError || 'PDF not yet compiled.'}
              </p>
              <button className="btn btn-primary" onClick={compilePdf} disabled={compiling}>
                {compiling ? 'Compiling...' : 'Compile PDF'}
              </button>
            </div>
          )
        ) : (
          <pre className="pv-latex-source">{latexSource || 'No LaTeX source available.'}</pre>
        )}
      </div>

      {/* Version bar */}
      <div className="pv-version-bar">
        <span className="pv-version-label">Paper v{paperVersion}</span>
        <span className="pv-version-sep">·</span>
        <span className="pv-version-label">{lineCount} lines</span>
        {isRewriting && (
          <>
            <span className="pv-version-sep">→</span>
            <span className="pv-version-label" style={{ color: 'var(--primary)', fontWeight: 500 }}>
              v{paperVersion + 1} generating...
            </span>
          </>
        )}
        {!isRewriting && pdfPath && <span className="pv-compiled-badge">Compiled</span>}
      </div>

      {/* Rewrite overlay */}
      {isRewriting && (
        <RewriteOverlay theoryStatus={theoryStatus} writerStatus={writerStatus} />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/PaperViewer.tsx
git commit -m "feat(ui): add PaperViewer with PDF/LaTeX tabs and version bar"
```

---

### Task 8: QAChat Component

**Files:**
- Create: `frontend/src/components/workspace/paper-review/QAChat.tsx`

- [ ] **Step 1: Create QAChat component**

```typescript
import { useState, useRef, useEffect } from 'react';
import { apiPost } from '@/api/client';
import { ChatMessage } from './ChatMessage';
import type { SessionRun, QAMessage } from '@/types';

interface QAChatProps {
  run: SessionRun;
  messages: QAMessage[];
  setMessages: React.Dispatch<React.SetStateAction<QAMessage[]>>;
  isRewriting: boolean;
  onAccept: () => void;
  onRewrite: (prompt: string) => void;
}

interface AskResponse {
  answer: string;
  tool_steps?: { tool: string; input: string; status: string }[];
  error?: string;
}

export function QAChat({ run, messages, setMessages, isRewriting, onAccept, onRewrite }: QAChatProps) {
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [rewriteMode, setRewriteMode] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;

    if (rewriteMode) {
      setRewriteMode(false);
      setInput('');
      onRewrite(text);
      return;
    }

    const userMsg: QAMessage = {
      role: 'user',
      content: text,
      ts: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setSending(true);

    try {
      const history = messages.map((m) => ({ role: m.role, content: m.content }));
      const res = await apiPost<AskResponse>(`/api/runs/${run.run_id}/paper-qa/ask`, {
        question: text,
        history,
      });

      const agentMsg: QAMessage = {
        role: 'assistant',
        content: res.answer || res.error || 'No response',
        ts: new Date().toISOString(),
        tool_steps: res.tool_steps?.map((s) => ({
          tool: s.tool,
          input: s.input,
          status: s.status as 'done' | 'running' | 'pending' | 'failed',
        })),
      };
      setMessages((prev) => [...prev, agentMsg]);
    } catch (e) {
      const errorMsg: QAMessage = {
        role: 'assistant',
        content: `Error: ${e instanceof Error ? e.message : String(e)}`,
        ts: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleRewriteClick() {
    setRewriteMode(true);
    setInput('');
  }

  const placeholder = isRewriting
    ? 'Waiting for rewrite to complete...'
    : rewriteMode
    ? 'Describe what to fix...'
    : 'Ask about the paper...';

  return (
    <div className="qa-chat">
      <div className="qa-chat-header">
        <span className="qa-chat-title">Paper Q&A</span>
        <span className={`qa-chat-badge${isRewriting ? ' qa-chat-badge--rewriting' : ''}`}>
          {isRewriting ? 'Rewriting...' : `${messages.length} messages`}
        </span>
      </div>

      <div className="qa-messages">
        {messages.map((msg, i) => (
          <ChatMessage key={i} message={msg} />
        ))}
        {sending && (
          <div className="qa-msg-wrap qa-msg-wrap--agent">
            <div className="tool-steps">
              <div className="tool-step">
                <span className="tool-step-dot tool-step-dot--running" />
                <span>Thinking...</span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className={`qa-input-area${isRewriting ? ' qa-input-area--disabled' : ''}`}>
        <div className="qa-input-row">
          <textarea
            className="qa-input-field"
            placeholder={placeholder}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            disabled={isRewriting || sending}
          />
          <button
            className="qa-send-btn"
            onClick={handleSend}
            disabled={isRewriting || sending || !input.trim()}
          >
            {rewriteMode ? 'Rewrite' : 'Send'}
          </button>
        </div>
        {!rewriteMode && (
          <div className="qa-action-row">
            <button className="qa-accept-btn" onClick={onAccept} disabled={isRewriting || sending}>
              ✓ Accept Paper
            </button>
            <button className="qa-rewrite-btn" onClick={handleRewriteClick} disabled={isRewriting || sending}>
              ↻ Rewrite Paper
            </button>
          </div>
        )}
        {rewriteMode && (
          <div className="qa-action-row">
            <button className="qa-accept-btn" onClick={() => setRewriteMode(false)}>
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/QAChat.tsx
git commit -m "feat(ui): add QAChat with multi-turn conversation and accept/rewrite actions"
```

---

### Task 9: PaperReviewPanel — Main Container

**Files:**
- Create: `frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx`

- [ ] **Step 1: Create PaperReviewPanel component**

```typescript
import { useState, useEffect, useCallback, useRef } from 'react';
import { apiGet, apiPost } from '@/api/client';
import { PaperViewer } from './PaperViewer';
import { QAChat } from './QAChat';
import type { SessionRun, QAMessage } from '@/types';

interface PaperReviewPanelProps {
  run: SessionRun;
}

interface HistoryResponse {
  messages: QAMessage[];
}

const SPLIT_KEY = 'eurekaclaw-review-split';
const MIN_SPLIT = 30;
const MAX_SPLIT = 70;

export function PaperReviewPanel({ run }: PaperReviewPanelProps) {
  const [messages, setMessages] = useState<QAMessage[]>([]);
  const [splitPct, setSplitPct] = useState(() => {
    const saved = localStorage.getItem(SPLIT_KEY);
    return saved ? Number(saved) : 55;
  });
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Determine rewrite state from pipeline
  const theoryTask = run.pipeline?.find((t) => t.name === 'theory');
  const writerTask = run.pipeline?.find((t) => t.name === 'writer');
  const isRewriting =
    theoryTask?.status === 'in_progress' ||
    theoryTask?.status === 'running' ||
    theoryTask?.status === 'pending' ||
    writerTask?.status === 'in_progress' ||
    writerTask?.status === 'running' ||
    writerTask?.status === 'pending' ||
    false;

  // Paper version = count of completed writer runs (from outputs)
  const paperVersion = writerTask?.outputs?.text_summary ? 1 : 1;

  // Load history on mount
  useEffect(() => {
    void (async () => {
      try {
        const data = await apiGet<HistoryResponse>(`/api/runs/${run.run_id}/paper-qa/history`);
        if (data.messages?.length) setMessages(data.messages);
      } catch {
        // No history yet
      }
    })();
  }, [run.run_id]);

  // Accept paper
  const handleAccept = useCallback(async () => {
    try {
      await apiPost(`/api/runs/${run.run_id}/gate/paper_qa`, { action: 'no', question: '' });
    } catch (e) {
      console.error('Failed to accept paper:', e);
    }
  }, [run.run_id]);

  // Rewrite paper
  const handleRewrite = useCallback(async (prompt: string) => {
    const sysMsg: QAMessage = {
      role: 'system',
      content: `↻ Rewrite requested: "${prompt}"`,
      ts: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, sysMsg]);
    try {
      await apiPost(`/api/runs/${run.run_id}/gate/paper_qa`, {
        action: 'rewrite',
        question: prompt,
      });
    } catch (e) {
      console.error('Failed to trigger rewrite:', e);
    }
  }, [run.run_id]);

  // Resizable divider drag handling
  const handleMouseDown = useCallback(() => {
    setIsDragging(true);
  }, []);

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
      localStorage.setItem(SPLIT_KEY, String(splitPct));
    }

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, [isDragging, splitPct]);

  return (
    <div
      className="paper-review-panel"
      ref={containerRef}
      style={{ userSelect: isDragging ? 'none' : undefined }}
    >
      <div style={{ flex: `0 0 ${splitPct}%`, minWidth: 0, display: 'flex' }}>
        <PaperViewer
          run={run}
          paperVersion={paperVersion}
          isRewriting={isRewriting}
          theoryStatus={theoryTask?.status || 'pending'}
          writerStatus={writerTask?.status || 'pending'}
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
          run={run}
          messages={messages}
          setMessages={setMessages}
          isRewriting={isRewriting}
          onAccept={handleAccept}
          onRewrite={handleRewrite}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/workspace/paper-review/PaperReviewPanel.tsx
git commit -m "feat(ui): add PaperReviewPanel with resizable divider"
```

---

### Task 10: Wire Panel into Workspace + Update GateOverlay

**Files:**
- Modify: `frontend/src/components/workspace/WorkspaceTabs.tsx`
- Modify: `frontend/src/components/workspace/GateOverlay.tsx:248-349,370-374`
- Modify: `frontend/src/hooks/usePolling.ts:66-70`

- [ ] **Step 1: Update WorkspaceTabs to conditionally render PaperReviewPanel**

Replace the entire content of `frontend/src/components/workspace/WorkspaceTabs.tsx`:

```typescript
import { useUiStore } from '@/store/uiStore';
import { LivePanel } from './LivePanel';
import { ProofPanel } from './ProofPanel';
import { PaperPanel } from './PaperPanel';
import { LogsPanel } from './LogsPanel';
import { PaperReviewPanel } from './paper-review/PaperReviewPanel';
import type { SessionRun } from '@/types';

interface WorkspaceTabsProps {
  run: SessionRun | null;
}

const TABS = [
  { key: 'live', label: 'Live' },
  { key: 'proof', label: 'Proof' },
  { key: 'paper', label: 'Paper' },
  { key: 'logs', label: 'Logs' },
] as const;

type TabKey = typeof TABS[number]['key'];

export function WorkspaceTabs({ run }: WorkspaceTabsProps) {
  const activeWsTab = useUiStore((s) => s.activeWsTab);
  const setActiveWsTab = useUiStore((s) => s.setActiveWsTab);

  // Full-panel takeover when paper_qa_gate is active
  const paperQATask = run?.pipeline?.find((t) => t.name === 'paper_qa_gate');
  const isReviewActive = paperQATask?.status === 'awaiting_gate';

  if (isReviewActive && run) {
    return (
      <div className="workspace-main-col">
        <PaperReviewPanel run={run} />
      </div>
    );
  }

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
      <div className={`ws-panel${activeWsTab === 'logs' ? ' is-visible' : ''}`} id="ws-panel-logs" role="tabpanel">
        <LogsPanel run={run} />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Remove PaperQAGate from GateOverlay**

In `frontend/src/components/workspace/GateOverlay.tsx`:

Delete the entire `PaperQAGate` function (lines 248-349).

Then in the `GateOverlay` export function, remove the paper_qa detection and rendering. Change lines 357-363:

```typescript
  const activeGate =
    surveyTask?.status === 'awaiting_gate' ? 'survey' :
    dirTask?.status === 'awaiting_gate' ? 'direction' :
    theoryTask?.status === 'awaiting_gate' ? 'theory' :
    null;
```

And remove line 374:
```typescript
        {activeGate === 'paper_qa' && <PaperQAGate run={run} />}
```

Also remove the `paperQATask` variable declaration (line 357).

- [ ] **Step 3: Add auto-switch to usePolling when paper_qa_gate activates**

In `frontend/src/hooks/usePolling.ts`, after line 70 (`if (gateJustActivated) setActiveWsTab('proof');`), add:

```typescript
        // Auto-switch when paper QA gate activates (panel takes over workspace)
        const paperQAGateTask = current.pipeline?.find((t) => t.name === 'paper_qa_gate');
        const prevPaperQAGateTask = prev?.pipeline?.find((t) => t.name === 'paper_qa_gate');
        const paperGateJustActivated = prevPaperQAGateTask?.status !== 'awaiting_gate' && paperQAGateTask?.status === 'awaiting_gate';
        if (paperGateJustActivated) setActiveWsTab('paper');
```

- [ ] **Step 4: Build and verify**

Run: `cd /Users/chenggong/Downloads/EurekaClaw/frontend && npx tsc --noEmit && npx vite build 2>&1 | tail -5`
Expected: Build succeeds with no TypeScript errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/workspace/WorkspaceTabs.tsx frontend/src/components/workspace/GateOverlay.tsx frontend/src/hooks/usePolling.ts
git commit -m "feat(ui): wire PaperReviewPanel into workspace, remove modal gate"
```

---

### Task 11: Frontend Build Verification

**Files:**
- No new files

- [ ] **Step 1: Build the frontend**

Run: `cd /Users/chenggong/Downloads/EurekaClaw/frontend && npm run build 2>&1 | tail -10`
Expected: Build succeeds

- [ ] **Step 2: Verify all imports resolve**

Run: `cd /Users/chenggong/Downloads/EurekaClaw/frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Copy built assets to static dir**

Run: `cp -r /Users/chenggong/Downloads/EurekaClaw/frontend/dist/* /Users/chenggong/Downloads/EurekaClaw/eurekaclaw/ui/static/`

- [ ] **Step 4: Commit built assets**

```bash
git add eurekaclaw/ui/static/ frontend/src/
git commit -m "build: compile frontend with paper review panel"
```

---

### Task 12: Backend Integration Smoke Test

**Files:**
- No new files

- [ ] **Step 1: Verify server imports**

Run:
```bash
cd /Users/chenggong/Downloads/EurekaClaw && source .venv/bin/activate && python -c "
from eurekaclaw.ui.server import EurekaRequestHandler
print('Server imports OK')
"
```
Expected: `Server imports OK`

- [ ] **Step 2: Run all existing backend tests**

Run: `cd /Users/chenggong/Downloads/EurekaClaw && source .venv/bin/activate && python -m pytest tests/ -v 2>&1 | tail -5`
Expected: Same results as before (135 passed, 1 failed pre-existing, 3 skipped)

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git diff --cached --quiet || git commit -m "chore: integration verification for paper review UI"
```
