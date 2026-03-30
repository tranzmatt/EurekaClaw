"""Novita backend wrapper built on the OpenAI-compatible adapter."""

from __future__ import annotations

from eurekaclaw.llm.openai_compat import OpenAICompatAdapter


class NovitaAdapter(OpenAICompatAdapter):
    """OpenAI-compatible Novita endpoint with provider defaults."""

    def __init__(self, api_key: str, default_model: str = "moonshotai/kimi-k2.5") -> None:
        super().__init__(
            base_url="https://api.novita.ai/openai",
            api_key=api_key,
            default_model=default_model,
        )
