from __future__ import annotations

import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any


class GeneratorError(Exception):
    """Raised when a generator logs an error — causes the Infrahub task to fail."""


class FailOnErrorLogger(logging.Logger):
    """Logger subclass that raises GeneratorError when .error() is called."""

    def error(
        self,
        msg: object,
        *args: object,
        exc_info: bool
        | tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | BaseException
        | None = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        super().error(msg, *args, exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel, extra=extra)
        formatted = str(msg) if not args else str(msg) % args
        raise GeneratorError(formatted)


class FailOnErrorLoggerMixin:
    """Mixin that replaces self.logger with FailOnErrorLogger on init.

    Any self.logger.error() call in a generator will raise GeneratorError,
    causing the Infrahub task to show as failed rather than silently green.
    """

    logger: logging.Logger

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger.__class__ = FailOnErrorLogger
