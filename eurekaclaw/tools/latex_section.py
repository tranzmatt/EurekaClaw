"""latex_section_read tool — extract sections from paper LaTeX by name or number."""

from __future__ import annotations

import re
import logging
from typing import Any

from eurekaclaw.knowledge_bus.bus import KnowledgeBus
from eurekaclaw.tools.base import BaseTool

logger = logging.getLogger(__name__)

# Regex matching \section{...}, \subsection{...}, \subsubsection{...}
_HEADING_RE = re.compile(
    r"\\(section|subsection|subsubsection)\*?\{([^}]+)\}",
)

_LEVEL = {"section": 0, "subsection": 1, "subsubsection": 2}


class LatexSectionReadTool(BaseTool):
    """Read a specific section from the paper LaTeX source by name or number."""

    name = "latex_section_read"
    description = (
        "Read a specific section from the paper's LaTeX source. "
        "Pass a section name (e.g. 'Introduction', 'Theorem 2') or "
        "number (e.g. '3', '3.1'). Returns the LaTeX content of that section."
    )

    def __init__(self, bus: KnowledgeBus) -> None:
        self.bus = bus

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "Section name (e.g. 'Introduction', 'Theorem 2') "
                        "or number (e.g. '3', '3.1') to extract."
                    ),
                },
            },
            "required": ["section"],
        }

    async def call(self, section: str) -> str:
        latex = self.bus.get("paper_qa_latex") or ""
        if not latex:
            return "Error: no paper LaTeX available on the bus."
        return self._extract_section(latex, section.strip())

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _extract_section(self, latex: str, query: str) -> str:
        headings = self._parse_headings(latex)
        if not headings:
            return "No sections found in the LaTeX source."

        # Try matching by dotted number (e.g. "3", "2.1")
        match_idx = self._match_by_number(headings, query)
        if match_idx is None:
            # Try matching by name (case-insensitive substring)
            match_idx = self._match_by_name(headings, query)

        if match_idx is None:
            names = [h["title"] for h in headings]
            return (
                f"No section matching '{query}'. "
                f"Available sections: {', '.join(names)}"
            )

        return self._slice_content(latex, headings, match_idx)

    def _parse_headings(self, latex: str) -> list[dict]:
        """Return list of {level, title, number, start_pos}."""
        headings: list[dict] = []
        counters = [0, 0, 0]  # section, subsection, subsubsection

        for m in _HEADING_RE.finditer(latex):
            level = _LEVEL[m.group(1)]
            title = m.group(2).strip()

            # Increment counter at this level, reset deeper levels
            counters[level] += 1
            for deeper in range(level + 1, 3):
                counters[deeper] = 0

            # Build dotted number: "3" or "3.1" or "3.1.2"
            parts = [str(counters[i]) for i in range(level + 1)]
            number = ".".join(parts)

            headings.append({
                "level": level,
                "title": title,
                "number": number,
                "start": m.start(),
            })
        return headings

    def _match_by_number(self, headings: list[dict], query: str) -> int | None:
        if not re.match(r"^\d+(\.\d+)*$", query):
            return None
        for i, h in enumerate(headings):
            if h["number"] == query:
                return i
        return None

    def _match_by_name(self, headings: list[dict], query: str) -> int | None:
        q = query.lower()
        for i, h in enumerate(headings):
            if q == h["title"].lower():
                return i
        # Fallback: substring match
        for i, h in enumerate(headings):
            if q in h["title"].lower():
                return i
        return None

    def _slice_content(self, latex: str, headings: list[dict], idx: int) -> str:
        """Extract content from the matched heading to the next same-or-higher-level heading."""
        start = headings[idx]["start"]
        level = headings[idx]["level"]

        end = len(latex)
        for h in headings[idx + 1:]:
            if h["level"] <= level:
                end = h["start"]
                break

        return latex[start:end].strip()
