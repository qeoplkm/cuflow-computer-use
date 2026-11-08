"""Provider-neutral LLM interface for the discovery agent.

The agent is *stateless per step*: every turn sends the goal, a compact action history and
the current observation, and asks for exactly one tool call. That keeps context bounded
(long flows do not grow the prompt), makes every decision reproducible from its logged
prompt, and lets us swap providers behind one method.

Implementations: Gemini (google-genai), Claude (anthropic), and a scripted fake used by
offline tests. Only discovery uses a model. Replay never imports this module.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class LLMReply:
    call: ToolCall | None
    text: str = ""
    model: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    model: str

    def decide(self, system: str, prompt: str, image: bytes | None, tools: list[ToolSpec]) -> LLMReply: ...


# --------------------------------------------------------------------------------------


class GeminiClient:
    def __init__(self, model: str = "gemini-3.5-flash", api_key: str | None = None) -> None:
        from google import genai

        self.model = model
        self._client = genai.Client(api_key=api_key or os.environ["GOOGLE_API_KEY"])

    def decide(self, system: str, prompt: str, image: bytes | None, tools: list[ToolSpec]) -> LLMReply:
        from google.genai import types

        decls = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            temperature=0,
        )
        parts = [types.Part.from_text(text=prompt)]
        if image:
            parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
        t0 = time.time()
        resp = self._client.models.generate_content(
            model=self.model, contents=[types.Content(role="user", parts=parts)], config=config
        )
        ms = int((time.time() - t0) * 1000)
        calls = resp.function_calls or []
        usage = {}
        if resp.usage_metadata:
            usage = {
                "input_tokens": resp.usage_metadata.prompt_token_count,
                "output_tokens": resp.usage_metadata.candidates_token_count,
            }
        text = "".join(part.text or "" for candidate in (resp.candidates or [])
                       for part in (candidate.content.parts if candidate.content else []) if part.text)
        call = ToolCall(calls[0].name, dict(calls[0].args or {})) if calls else None
        return LLMReply(call=call, text=text, model=self.model, latency_ms=ms, usage=usage)


class ClaudeClient:
    """Claude via the Anthropic SDK. Forced tool choice is not available on current models,
    so we use tool_choice=auto and instruct the model to always call exactly one tool."""

    def __init__(self, model: str = "claude-opus-5-5") -> None:
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic()

    def decide(self, system: str, prompt: str, image: bytes | None, tools: list[ToolSpec]) -> LLMReply:
        content: list[dict[str, Any]] = []
        if image:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image).decode()},
                }
            )
        content.append({"type": "text", "text": prompt})
        t0 = time.time()
        resp = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system + "\n\nAlways respond by calling exactly one tool.",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
            tool_choice={"type": "auto"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        )
        ms = int((time.time() - t0) * 1000)
        if resp.stop_reason == "refusal":
            return LLMReply(call=None, text="(model declined)", model=resp.model, latency_ms=ms)
        call = None
        text = ""
        for block in resp.content:
            if block.type == "tool_use" and call is None:
                call = ToolCall(block.name, dict(block.input))
            elif block.type == "text":
                text += block.text
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return LLMReply(call=call, text=text, model=resp.model, latency_ms=ms, usage=usage)


class ScriptedLLM:
    """Deterministic stand-in for tests: replays a script of tool calls.

    A ref may be written as ``@<role> "<name>"`` (or ``@text "<text>"``) and is resolved
    against the current observation text, so scripts do not depend on ref numbering.
    """

    model = "scripted"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)

    def decide(self, system: str, prompt: str, image: bytes | None, tools: list[ToolSpec]) -> LLMReply:
        if not self.script:
            return LLMReply(call=ToolCall("escalate", {"reason": "script exhausted", "kind": "stuck"}), model=self.model)
        step = dict(self.script.pop(0))
        args = dict(step.get("args", {}))
        ref = args.get("ref")
        if isinstance(ref, str) and ref.startswith("@"):
            m = re.match(r'@(\w+) "(.*)"$', ref)
            assert m, ref
            role, name = m.groups()
            screen = prompt.split("CURRENT SCREEN:", 1)[-1]
            pat = (
                rf"\[([\w:]+)\] {re.escape(name)}(?:\s|$|\|)"
                if role == "text"
                else rf'\[([\w:]+)\] {re.escape(role)} "{re.escape(name)}"'
            )
            found = re.search(pat, screen, re.M)
            args["ref"] = found.group(1) if found else "missing:0"
        return LLMReply(call=ToolCall(step["tool"], args), model=self.model)


def make_llm(provider: str, model: str | None = None) -> LLMClient:
    if provider == "gemini":
        return GeminiClient(model or os.environ.get("CUFLOW_GEMINI_MODEL", "gemini-3.5-flash"))
    if provider in ("claude", "anthropic"):
        return ClaudeClient(model or "claude-opus-5-5")
    raise ValueError(f"unknown provider {provider!r}")
