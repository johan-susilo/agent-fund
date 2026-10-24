"""Generic tool-calling loop (Gemini function calling) shared by every LLM agent."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

MAX_TOOL_CHARS = 7000


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    fn: Callable
    required: list = field(default_factory=list)


def _declarations(tools):
    from google.genai import types
    return [types.FunctionDeclaration(
        name=t.name, description=t.description,
        parameters={"type": "OBJECT", "properties": t.properties, "required": t.required})
        for t in tools]


def _run_tool(tool, args: dict) -> dict:
    if tool is None:
        return {"error": "unknown tool"}
    try:
        out = json.loads(json.dumps(tool.fn(**args), ensure_ascii=False, default=str))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    s = json.dumps(out, ensure_ascii=False)
    return {"truncated": True, "data": s[:MAX_TOOL_CHARS]} if len(s) > MAX_TOOL_CHARS else out


def _add_usage(usage: dict, resp) -> None:
    u = getattr(resp, "usage_metadata", None)
    usage["input_tokens"] += int(getattr(u, "prompt_token_count", 0) or 0)
    usage["output_tokens"] += int((getattr(u, "candidates_token_count", 0) or 0)
                                  + (getattr(u, "thoughts_token_count", 0) or 0))


def run_agent_loop(system_prompt: str, user_prompt: str, tools: list, max_steps: int = 8,
                   max_calls: int = 14, temperature=None):
    """Model calls tools until it answers in plain text.
    Returns (final_text, usage, n_tool_calls, models_used)."""
    from google.genai import types
    import llm

    by_name = {t.name: t for t in tools}
    temp = float(os.environ.get("LLM_TEMPERATURE", 0.2 if temperature is None else temperature))
    cfg = types.GenerateContentConfig(
        system_instruction=system_prompt, temperature=temp,
        tools=[types.Tool(function_declarations=_declarations(tools))],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]
    usage, models, n_calls, text = {"input_tokens": 0, "output_tokens": 0}, [], 0, ""

    for _ in range(max_steps):
        resp, model = llm.generate_raw(contents, cfg)
        _add_usage(usage, resp)
        if model not in models:
            models.append(model)
        content = resp.candidates[0].content if resp.candidates else None
        parts = (content.parts or []) if content else []
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        text = "".join(p.text for p in parts
                       if getattr(p, "text", None) and not getattr(p, "thought", False)) or text
        if not calls:
            return text, usage, n_calls, models
        contents.append(content)
        replies = []
        for c in calls:
            if n_calls >= max_calls:
                out = {"error": "tool-call budget exhausted; write your final answer now"}
            else:
                n_calls += 1
                out = _run_tool(by_name.get(c.name), dict(c.args or {}))
            replies.append(types.Part.from_function_response(name=c.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=replies))

    contents.append(types.Content(role="user", parts=[types.Part(
        text="Step budget exhausted. Write your final answer now, without calling tools.")]))
    resp, model = llm.generate_raw(contents, types.GenerateContentConfig(
        system_instruction=system_prompt, temperature=temp))
    _add_usage(usage, resp)
    return (resp.text or text), usage, n_calls, models
