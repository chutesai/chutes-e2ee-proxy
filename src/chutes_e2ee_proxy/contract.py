from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


async def normalize_json_request_body(
    headers: Mapping[str, str],
    body: bytes,
    *,
    canonicalize_model: Callable[[str], Awaitable[str | None]] | None = None,
) -> bytes:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    if not isinstance(payload, dict):
        return body

    changed = False
    model = payload.get("model")
    if not isinstance(model, str):
        return body

    enable_thinking = False
    if model.endswith(":THINKING"):
        model = model[: -len(":THINKING")]
        enable_thinking = True
        changed = True
    if canonicalize_model is not None:
        canonical_model = await canonicalize_model(model)
        if canonical_model and canonical_model != model:
            model = canonical_model
            changed = True
    if _is_true(headers.get("X-Enable-Thinking")):
        enable_thinking = True

    if payload.get("continue_final_message") and payload.get("add_generation_prompt", True):
        messages = payload.get("messages", [])
        if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
            if messages[-1].get("role") == "assistant":
                payload["add_generation_prompt"] = False
            else:
                payload["continue_final_message"] = False
            changed = True

    if payload.get("tools") and "tool_choice" not in payload:
        payload["tool_choice"] = "auto"
        changed = True

    chat_template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(chat_template_kwargs, dict):
        if "thinking" in chat_template_kwargs and "enable_thinking" not in chat_template_kwargs:
            chat_template_kwargs["enable_thinking"] = chat_template_kwargs["thinking"]
            changed = True
        if "enable_thinking" in chat_template_kwargs and "thinking" not in chat_template_kwargs:
            chat_template_kwargs["thinking"] = chat_template_kwargs["enable_thinking"]
            changed = True

    if enable_thinking:
        if not isinstance(chat_template_kwargs, dict):
            payload["chat_template_kwargs"] = {}
            chat_template_kwargs = payload["chat_template_kwargs"]
            changed = True
        if chat_template_kwargs.get("thinking") is not True:
            chat_template_kwargs["thinking"] = True
            changed = True
        if chat_template_kwargs.get("enable_thinking") is not True:
            chat_template_kwargs["enable_thinking"] = True
            changed = True

    if payload.get("model") != model:
        payload["model"] = model
        changed = True

    if not changed:
        return body
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
