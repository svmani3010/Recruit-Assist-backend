"""LLM error types — preserve original exception chain."""

from __future__ import annotations


class LLMError(Exception):
    """Base class for LLM layer failures."""


class LLMTimeoutError(LLMError):
    """Request exceeded configured timeout."""


class LLMResponseError(LLMError):
    """Model returned empty, invalid, or unparseable content."""


class LLMConfigError(LLMError):
    """Missing prompt, model, or configuration."""
