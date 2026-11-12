from __future__ import annotations

import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any


class GeneratorError(Exception):
    """Raised when a generator logs an error, which fails the Infrahub task."""


class FailOnErrorLogger(logging.Logger):
    """Logger subclass that raises GeneratorError on .error()."""

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
    """Mixin that swaps self.logger for FailOnErrorLogger, so an error log fails the task."""

    logger: logging.Logger

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger.__class__ = FailOnErrorLogger
