import logging
from typing import Any


logger = logging.getLogger("openai-compat")

_RESPONSES_SUPPORT_CACHE: dict[int, bool] = {}


def request_text(client: Any, *, model: str, instructions: str, input_text: str) -> str:
    client_id = id(client)
    if _RESPONSES_SUPPORT_CACHE.get(client_id, True):
        try:
            response = client.responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            _RESPONSES_SUPPORT_CACHE[client_id] = False
            logger.warning("responses api unsupported for current base_url, fallback to chat completions")
        else:
            _RESPONSES_SUPPORT_CACHE[client_id] = True
            return (response.output_text or "").strip()

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": input_text},
        ],
    )
    message = (completion.choices or [None])[0]
    if not message or not getattr(message, "message", None):
        return ""
    content = message.message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return str(content or "").strip()
