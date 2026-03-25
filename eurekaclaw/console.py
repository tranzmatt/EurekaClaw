from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import Any

from rich.console import Console

# Singleton instance with record=True to capture all terminal output
# including all stdout, stderr, and Python logging (via RichHandler).
console = Console(record=True)

_plain_console = Console(file=io.StringIO(), color_system=None, force_terminal=False, width=120)
_thread_state = threading.local()
_original_print = console.print


class _UiHtmlLineSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._started = False
        self._closed = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        self.path.write_text(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>EurekaClaw UI Launch Transcript</title>"
            "<style>body{font-family:ui-monospace,Menlo,monospace;background:#f8f6f1;color:#1f2937;"
            "padding:24px;}.log{white-space:pre-wrap;word-break:break-word;line-height:1.45;}"
            ".log-line{margin:0 0 2px;}</style>"
            "</head><body><div class='log'>\n",
            encoding="utf-8",
        )
        self._started = True

    def write_fragment(self, fragment: str) -> None:
        if self._closed:
            return
        self._ensure_started()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("<div class='log-line'>")
            fh.write(fragment)
            fh.write("</div>")
            fh.write("\n")

    def close(self) -> None:
        if self._closed:
            return
        self._ensure_started()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("</div></body></html>\n")
        self._closed = True


def _get_thread_sink() -> _UiHtmlLineSink | None:
    return getattr(_thread_state, "ui_html_sink", None)


def register_ui_html_sink(path: str | Path) -> Path:
    sink = _UiHtmlLineSink(path)
    _thread_state.ui_html_sink = sink
    sink._ensure_started()
    return sink.path


def close_ui_html_sink() -> None:
    sink = _get_thread_sink()
    if sink is None:
        return
    sink.close()
    _thread_state.ui_html_sink = None


def _render_html_fragment(*args: Any, **kwargs: Any) -> str:
    rich_console = Console(record=True, width=120)
    rich_console.print(*args, **kwargs)
    return rich_console.export_html(inline_styles=True, code_format="{code}")


def _print_and_mirror(*args: Any, **kwargs: Any) -> None:
    _original_print(*args, **kwargs)
    sink = _get_thread_sink()
    if sink is None:
        return
    try:
        fragment = _render_html_fragment(*args, **kwargs).strip()
    except Exception:
        return
    if fragment:
        sink.write_fragment(fragment)


console.print = _print_and_mirror


class UiHtmlLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        sink = _get_thread_sink()
        if sink is None:
            return
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        color = {
            logging.DEBUG: "#6b7280",
            logging.INFO: "#1f2937",
            logging.WARNING: "#b45309",
            logging.ERROR: "#b91c1c",
            logging.CRITICAL: "#7f1d1d",
        }.get(record.levelno, "#1f2937")
        fragment = f"<span style='color: {color}'>{message}</span>"
        sink.write_fragment(fragment)
