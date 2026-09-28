"""Retry / timeout helpers for Gemini Interactions API calls."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Optional

from google import genai

from app.llm.errors import LLMError, LLMResponseError, LLMTimeoutError

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, FuturesTimeout, LLMTimeoutError)):
        return True
    if isinstance(exc, genai.errors.ServerError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        return code in {429, 500, 502, 503, 504}
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("429", "503", "502", "504", "timeout", "unavailable", "rate")
    )


def create_interaction(
    *,
    client: Any,
    max_retries: int = 3,
    timeout_seconds: float = 90.0,
    **create_kwargs: Any,
) -> Any:
    """
    Call `client.interactions.create(**create_kwargs)` with a timeout and
    exponential backoff on retryable failures.

    Returns the raw `Interaction` response (`.id`, `.output_text`, `.steps`)
    so callers needing the interaction id (e.g. multi-turn L1 chat) don't have
    to make a second call. Preserves the exception cause chain via `from e`.
    """
    model = create_kwargs.get("model", "?")
    last_error: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.interactions.create, **create_kwargs)
                try:
                    return future.result(timeout=timeout_seconds)
                except FuturesTimeout as e:
                    future.cancel()
                    raise LLMTimeoutError(
                        f"Gemini call timed out after {timeout_seconds}s "
                        f"(model={model}, attempt={attempt + 1})"
                    ) from e
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1 and _is_retryable(e):
                wait = min(2**attempt, 16)
                logger.warning(
                    "Gemini retryable failure (attempt %s/%s): %s; sleeping %ss",
                    attempt + 1,
                    max_retries,
                    e,
                    wait,
                )
                time.sleep(wait)
                continue
            if isinstance(e, LLMError):
                raise
            raise LLMError(f"Gemini API call failed (model={model}): {e}") from e

    raise LLMError(f"Gemini API call failed after {max_retries} attempts") from last_error


def generate_json(
    *,
    client: Any,
    max_retries: int = 3,
    timeout_seconds: float = 90.0,
    parse_json: bool = True,
    **create_kwargs: Any,
) -> Any:
    """
    Call the Interactions API via `create_interaction` and return either the
    parsed JSON object (`parse_json=True`) or the raw output text
    (`parse_json=False`).
    """
    interaction = create_interaction(
        client=client,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        **create_kwargs,
    )
    text = (getattr(interaction, "output_text", None) or "").strip()
    if not text:
        raise LLMResponseError("Empty response from Gemini")
    if not parse_json:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMResponseError(
            f"Gemini returned non-JSON content: {text[:200]!r}"
        ) from e
