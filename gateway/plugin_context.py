"""Task-local context supplied by gateway runtime plugins."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping

_SUBPROCESS_ENV: ContextVar[dict[str, str] | None] = ContextVar(
    "gateway_plugin_subprocess_env", default=None
)


def subprocess_env_values() -> dict[str, str]:
    """Return the plugin-owned subprocess environment for the current turn."""
    return dict(_SUBPROCESS_ENV.get() or {})


@contextmanager
def scoped_subprocess_env(values: Mapping[str, str]) -> Iterator[None]:
    token: Token[dict[str, str] | None] = _SUBPROCESS_ENV.set(
        {str(key): str(value) for key, value in values.items()}
    )
    try:
        yield
    finally:
        _SUBPROCESS_ENV.reset(token)


@contextmanager
def cleared_subprocess_env() -> Iterator[None]:
    """Prevent delegated children from inheriting a parent plugin's routing."""
    with scoped_subprocess_env({}):
        yield
