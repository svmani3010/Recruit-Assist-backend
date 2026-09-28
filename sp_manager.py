"""System prompt loader with validation, optional hot-reload, and thread safety."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Repo root (this file lives next to the default `prompts/` directory).
_REPO_ROOT = Path(__file__).resolve().parent


def resolve_prompt_dir(prompt_dir: str) -> str:
    """Resolve a prompt directory against cwd, then the repository root."""
    path = Path(prompt_dir)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, _REPO_ROOT / path]
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate.resolve())
    return str((candidates[0]).resolve()) if candidates else str(path)


class PromptError(Exception):
    """Base error for prompt manager failures."""


class PromptNotFoundError(PromptError):
    """Requested prompt name is missing or empty."""


class PromptDirectoryError(PromptError):
    """Prompt directory missing or unreadable."""


class SystemPromptManager:
    """
    Loads ``*.md`` prompts from a directory into memory.

    - Skips empty files and logs errors
    - Optional mtime-based hot reload (no process restart required)
    - Thread-safe reads/reloads
    """

    def __init__(
        self,
        prompt_dir: str,
        *,
        auto_reload: bool = True,
        required: Optional[Sequence[str]] = None,
        encoding: str = "utf-8",
    ):
        self.prompt_dir = resolve_prompt_dir(prompt_dir)
        self.auto_reload = auto_reload
        self.encoding = encoding
        self._required = tuple(required or ())
        self._lock = threading.RLock()
        self._cache: Dict[str, str] = {}
        self._mtimes: Dict[str, float] = {}
        self._load_all_prompts(initial=True)
        self._assert_required()

    @property
    def prompts_cache(self) -> Mapping[str, str]:
        """Read-only view of the in-memory prompt map (compat with older callers)."""
        with self._lock:
            return dict(self._cache)

    def _iter_prompt_files(self) -> List[Path]:
        root = Path(self.prompt_dir)
        if not root.is_dir():
            raise PromptDirectoryError(f"Prompt directory not found: {self.prompt_dir}")
        return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".md")

    def _missing_required(self, cache: Mapping[str, str]) -> List[str]:
        return [name for name in self._required if not cache.get(name)]

    def _assert_required(self) -> None:
        missing = self._missing_required(self._cache)
        if missing:
            raise PromptNotFoundError(
                f"Required prompts missing or empty in {self.prompt_dir}: {missing}"
            )

    def _load_all_prompts(self, *, initial: bool = False) -> None:
        with self._lock:
            new_cache: Dict[str, str] = {}
            new_mtimes: Dict[str, float] = {}
            try:
                files = self._iter_prompt_files()
            except PromptDirectoryError as e:
                if initial:
                    logger.warning("%s — no prompts loaded", e)
                    self._cache = {}
                    self._mtimes = {}
                    return
                raise

            for path in files:
                name = path.stem
                try:
                    mtime = path.stat().st_mtime
                    # Always record mtime so empty/skipped files don't look "new" forever
                    new_mtimes[name] = mtime
                    text = path.read_text(encoding=self.encoding).strip()
                    if not text:
                        logger.error(
                            "Prompt file is empty and was skipped: %s", path
                        )
                        continue
                    new_cache[name] = text
                except OSError as e:
                    logger.error("Failed to read prompt file %s: %s", path, e)

            missing = self._missing_required(new_cache)
            if missing and not initial:
                logger.error(
                    "Prompt reload skipped; required prompts missing or empty: %s",
                    missing,
                )
                return

            self._cache = new_cache
            self._mtimes = new_mtimes
            logger.info(
                "Loaded %s prompt(s) from %s: %s",
                len(self._cache),
                self.prompt_dir,
                sorted(self._cache.keys()),
            )

    def _reload_if_stale(self) -> None:
        with self._lock:
            try:
                files = self._iter_prompt_files()
            except PromptDirectoryError:
                return

            current: Dict[str, float] = {}
            for path in files:
                try:
                    current[path.stem] = path.stat().st_mtime
                except OSError:
                    self._load_all_prompts()
                    return

            if current != self._mtimes:
                self._load_all_prompts()
                return

    def reload(self) -> Dict[str, str]:
        """Force reload all prompts from disk. Returns a copy of the cache."""
        self._load_all_prompts()
        self._assert_required()
        return dict(self.prompts_cache)

    def list_prompts(self) -> List[str]:
        with self._lock:
            if self.auto_reload:
                self._reload_if_stale()
            return sorted(self._cache.keys())

    def get_system_prompt(self, prompt_name: str) -> Optional[str]:
        with self._lock:
            if self.auto_reload:
                self._reload_if_stale()
            return self._cache.get(prompt_name)

    def require(self, prompt_name: str) -> str:
        prompt = self.get_system_prompt(prompt_name)
        if not prompt:
            raise PromptNotFoundError(
                f"System prompt '{prompt_name}' not found or empty in {self.prompt_dir}"
            )
        return prompt

    def __contains__(self, prompt_name: object) -> bool:
        if not isinstance(prompt_name, str):
            return False
        return self.get_system_prompt(prompt_name) is not None

    def __repr__(self) -> str:
        return (
            f"SystemPromptManager(prompt_dir={self.prompt_dir!r}, "
            f"count={len(self._cache)}, auto_reload={self.auto_reload})"
        )
