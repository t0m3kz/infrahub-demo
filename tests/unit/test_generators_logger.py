"""Unit tests for generators.logger."""

from __future__ import annotations

import logging

import pytest

from generators.logger import FailOnErrorLogger, FailOnErrorLoggerMixin, GeneratorError


class TestFailOnErrorLogger:
    def test_error_raises_generator_error(self) -> None:
        logger = FailOnErrorLogger("test-fail-on-error")

        with pytest.raises(GeneratorError, match="boom"):
            logger.error("boom")

    def test_error_formats_message_with_args(self) -> None:
        logger = FailOnErrorLogger("test-fail-on-error-format")

        with pytest.raises(GeneratorError, match="failed on node-1"):
            logger.error("failed on %s", "node-1")


class _Base:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-mixin-logger")


class _WithMixin(FailOnErrorLoggerMixin, _Base):
    pass


class TestFailOnErrorLoggerMixin:
    def test_mixin_replaces_logger_class(self) -> None:
        obj = _WithMixin()
        assert obj.logger.__class__ is FailOnErrorLogger
