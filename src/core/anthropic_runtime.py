"""Nghimmo Claude transport and NeMo adapter using Anthropic /v1/messages.

The service's Claude keys reject OpenAI Chat Completions. Both the main agent
and NeMo classifiers use this transport with the same configured model/key.
No provider failures are converted to fabricated completions.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx

from core.config import get_red_model
from core.openai_runtime import OpenAIAgent, OpenAIRunner


class NghimmoTransport:
    def __init__(self):
        self.model = get_red_model()
        self.base_url = os.environ.get("NGHIMMO_BASE_URL", "https://api.nghimmo.com").rstrip("/")
        self._key = os.environ.get("NGHIMMO_API_KEY", "").strip()
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or url.hostname != "api.nghimmo.com"
                or url.username or url.password or url.port not in (None, 443)
                or url.path or url.query or url.fragment):
            raise ValueError("NGHIMMO_BASE_URL must be https://api.nghimmo.com")
        if not self._key:
            raise RuntimeError("NGHIMMO_API_KEY is missing")
        if not self.model.startswith("nghi/claude-"):
            raise ValueError("The Nghimmo Claude route requires an nghi/claude-* model")
        self.calls = 0

    async def _request(self, body):
        self.calls += 1
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.post(
                    self.base_url + "/v1/messages",
                    headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
                    json=body,
                )
            if response.status_code != 200:
                # Vendor error bodies can echo credentials or request context.
                raise RuntimeError(f"Nghimmo request failed: HTTP {response.status_code}")
            return response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Nghimmo transport failed: {type(exc).__name__}") from None

    async def complete(self, *, system="", messages, max_tokens=768, temperature=0.4, stop=None):
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": messages}
        if system:
            body["system"] = system
        if stop:
            body["stop_sequences"] = stop
        data = await self._request(body)
        text = "".join(block.get("text", "") for block in data.get("content", [])
                       if block.get("type") == "text").strip()
        if not text:
            raise RuntimeError("Nghimmo returned no text response")
        return text


class ClaudeRunner(OpenAIRunner):
    """Reuse ADK-style plugin dispatch, with an actual Anthropic wire format."""
    def __init__(self, *, app_name, plugins=None, temperature=0.4):
        super().__init__(app_name=app_name, model=get_red_model(), provider="nghimmo",
                         plugins=plugins or [], temperature=temperature)
        self.transport = NghimmoTransport()

    async def chat(self, agent, user_message, *, user_id="student"):
        for hook in self.input_hooks:
            blocked = hook(user_message)
            if blocked:
                return blocked
        blocked = await self._run_input_plugins(user_message, user_id=user_id)
        if blocked is not None:
            return blocked
        text = await self.transport.complete(
            system=agent.instruction, messages=[{"role": "user", "content": user_message}],
            temperature=self.temperature,
        )
        for hook in self.output_hooks:
            text = hook(text)
        return await self._run_output_plugins(text)


def create_claude_pair(*, name, instruction, app_name, plugins=None, temperature=0.4):
    return (OpenAIAgent(name=name, instruction=instruction, provider="nghimmo"),
            ClaudeRunner(app_name=app_name, plugins=plugins, temperature=temperature))


class NghimmoNemoModel:
    """Implements NeMo 0.24 LLMModel; all rails still run inside LLMRails."""
    def __init__(self):
        self.transport = NghimmoTransport()

    @property
    def model_name(self):
        return self.transport.model

    @property
    def provider_name(self):
        return "nghimmo"

    @property
    def provider_url(self):
        return self.transport.base_url

    async def generate_async(self, prompt, *, stop=None, **kwargs):
        from nemoguardrails.types import LLMResponse
        if isinstance(prompt, str):
            system, messages = "", [{"role": "user", "content": prompt}]
        else:
            system_parts, messages = [], []
            for item in prompt:
                row = item if isinstance(item, dict) else item.to_dict()
                if row["role"] in ("system", "developer"):
                    system_parts.append(row.get("content") or "")
                else:
                    messages.append({"role": row["role"], "content": row.get("content") or ""})
            system = "\n".join(system_parts)
        text = await self.transport.complete(
            system=system, messages=messages, max_tokens=kwargs.get("max_tokens") or 128,
            temperature=kwargs.get("temperature", 0), stop=stop,
        )
        # A rail must return an unambiguous decision, otherwise fail closed.
        if text.strip().lower().rstrip(".! ") not in {"yes", "no"}:
            raise RuntimeError("NeMo classifier returned an ambiguous verdict")
        return LLMResponse(content=text)

    async def stream_async(self, prompt, *, stop=None, **kwargs):
        from nemoguardrails.types import LLMResponseChunk
        response = await self.generate_async(prompt, stop=stop, **kwargs)
        yield LLMResponseChunk(delta_content=response.content)
