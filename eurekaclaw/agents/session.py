"""AgentSession — context window management and conversation history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _coerce_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool:{block.get('name', '')}]")
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", "")))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


@dataclass
class SessionCompactionRecord:
    summary: str
    reason: str
    compaction_index: int
    original_message_count: int
    preserved_message_count: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    compacted_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "reason": self.reason,
            "compaction_index": self.compaction_index,
            "original_message_count": self.original_message_count,
            "preserved_message_count": self.preserved_message_count,
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "compacted_at": self.compacted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionCompactionRecord":
        return cls(
            summary=str(data.get("summary", "")),
            reason=str(data.get("reason", "")),
            compaction_index=int(data.get("compaction_index", 0)),
            original_message_count=int(data.get("original_message_count", 0)),
            preserved_message_count=int(data.get("preserved_message_count", 0)),
            estimated_tokens_before=int(data.get("estimated_tokens_before", 0)),
            estimated_tokens_after=int(data.get("estimated_tokens_after", 0)),
            compacted_at=str(data.get("compacted_at", "")),
        )


class AgentSession:
    """Manages the rolling conversation history for an agent's context window."""

    def __init__(self, max_tokens: int = 180_000) -> None:
        self.max_tokens = max_tokens
        self._messages: list[dict[str, Any]] = []
        self._token_count: int = 0
        self._compaction_history: list[SessionCompactionRecord] = []

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str | list[dict[str, Any]]) -> None:
        """Add an assistant turn. Content may be a plain string or a list of
        serialized content blocks (required when the turn contains tool_use)."""
        self._messages.append({"role": "assistant", "content": content})

    def add_tool_result(self, tool_use_id: str, content: str) -> None:
        self._messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        })

    def get_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def clear(self) -> None:
        self._messages.clear()
        self._token_count = 0
        self._compaction_history.clear()

    def estimated_tokens(self) -> int:
        """Very rough token estimate used only for compaction heuristics."""
        return sum(max(1, len(_coerce_text(m.get("content", ""))) // 4) for m in self._messages)

    def get_recent_messages(self, count: int) -> list[dict[str, Any]]:
        if count <= 0:
            return []
        return list(self._messages[-count:])

    def should_compact(
        self,
        *,
        max_messages: int = 40,
        token_threshold: int | None = None,
    ) -> bool:
        token_limit = token_threshold or self.max_tokens
        return len(self._messages) > max_messages or self.estimated_tokens() >= token_limit

    def trim_to_fit(self, max_messages: int = 40) -> None:
        """Keep only the most recent max_messages to avoid context overflow."""
        if len(self._messages) > max_messages:
            # Always keep the first user message as context anchor
            self._messages = self._messages[:1] + self._messages[-(max_messages - 1):]

    def compress_to_summary(
        self,
        original_task: str,
        summary: str,
        *,
        preserve_recent_messages: int = 6,
        reason: str = "manual",
    ) -> SessionCompactionRecord:
        """Compact older history into a summary while preserving a recent tail."""
        before_tokens = self.estimated_tokens()
        original_count = len(self._messages)
        preserved_tail = self.get_recent_messages(preserve_recent_messages)
        compressed = (
            f"{original_task}\n\n"
            "### Context Compaction Boundary\n"
            f"Older conversation history was summarized to control context growth. "
            f"Compaction reason: {reason}.\n\n"
            f"### Progress Summary\n"
            f"{summary}\n\n"
            "Continue from the compacted summary and the preserved recent messages below."
        )
        self._messages = [{"role": "user", "content": compressed}, *preserved_tail]
        self._token_count = 0
        record = SessionCompactionRecord(
            summary=summary,
            reason=reason,
            compaction_index=len(self._compaction_history) + 1,
            original_message_count=original_count,
            preserved_message_count=len(preserved_tail),
            estimated_tokens_before=before_tokens,
            estimated_tokens_after=self.estimated_tokens(),
            compacted_at=datetime.now(timezone.utc).isoformat(),
        )
        self._compaction_history.append(record)
        return record

    def latest_compaction(self) -> SessionCompactionRecord | None:
        return self._compaction_history[-1] if self._compaction_history else None

    def export_compact_state(self) -> dict[str, Any]:
        return {
            "messages": self.get_messages(),
            "estimated_tokens": self.estimated_tokens(),
            "compactions": [item.to_dict() for item in self._compaction_history],
        }

    def load_compact_state(self, data: dict[str, Any]) -> None:
        self._messages = list(data.get("messages", []))
        self._token_count = int(data.get("estimated_tokens", 0))
        self._compaction_history = [
            SessionCompactionRecord.from_dict(item)
            for item in data.get("compactions", [])
            if isinstance(item, dict)
        ]

    def __len__(self) -> int:
        return len(self._messages)
