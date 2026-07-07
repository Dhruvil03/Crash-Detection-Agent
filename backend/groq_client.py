"""
groq_client.py

Minimal async client for Groq's OpenAI-compatible chat completions
endpoint with tool-calling support.

Model note: llama-3.3-70b-versatile was deprecated by Groq on
2026-06-17. Default is now openai/gpt-oss-120b. Override via the
GROQ_MODEL env var or per-request groq_model field.
Check https://console.groq.com/docs/models for the current lineup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqClientError(Exception):
    pass


@dataclass
class GroqMessage:
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_api_dict(self) -> dict:
        d: dict = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        elif self.role != "tool":
            d["content"] = None
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @staticmethod
    def system(text: str) -> "GroqMessage":
        return GroqMessage(role="system", content=text)

    @staticmethod
    def user(text: str) -> "GroqMessage":
        return GroqMessage(role="user", content=text)

    @staticmethod
    def assistant(content: str | None, tool_calls: list[dict] | None) -> "GroqMessage":
        return GroqMessage(role="assistant", content=content, tool_calls=tool_calls)

    @staticmethod
    def tool_result(tool_call_id: str, name: str, content: str) -> "GroqMessage":
        return GroqMessage(role="tool", content=content, tool_call_id=tool_call_id, name=name)


@dataclass
class GroqConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    base_url: str = field(default_factory=lambda: os.environ.get("GROQ_BASE_URL", GROQ_CHAT_URL))
    timeout_seconds: float = 60.0


class GroqClient:
    def __init__(self, config: GroqConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def chat(self, messages: list[GroqMessage], tools: list[dict] | None = None) -> GroqMessage:
        if not self.config.api_key:
            raise GroqClientError("No Groq API key configured. Set GROQ_API_KEY in your environment.")

        payload: dict = {
            "model": self.config.model,
            "messages": [m.to_api_dict() for m in messages],
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        try:
            response = await self._client.post(self.config.base_url, json=payload, headers=headers)
        except httpx.RequestError as e:
            raise GroqClientError(f"network error calling Groq API: {e}") from e

        if response.status_code < 200 or response.status_code >= 300:
            try:
                msg = response.json().get("error", {}).get("message", response.text)
            except Exception:
                msg = response.text
            raise GroqClientError(f"Groq API error (HTTP {response.status_code}): {msg}")

        try:
            data = response.json()
        except Exception as e:
            raise GroqClientError(f"failed to decode Groq response: {e}") from e

        choices = data.get("choices", [])
        if not choices:
            raise GroqClientError("Groq response contained no choices")

        msg_data = choices[0]["message"]
        return GroqMessage(
            role=msg_data.get("role", "assistant"),
            content=msg_data.get("content"),
            tool_calls=msg_data.get("tool_calls"),
        )


def config_from_env() -> GroqConfig:
    return GroqConfig(
        api_key=os.environ.get("GROQ_API_KEY", ""),
        model=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
    )
