"""Unit tests for the failing logger of generators/base.py."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from infrahub_sdk.generator import InfrahubGenerator

from generators.base import BaseGenerator, FailOnErrorLogger
from generators.model.errors import GeneratorError


class TestFailOnErrorLogger:
    def test_error_raises_generator_error(self) -> None:
        logger = FailOnErrorLogger("test-fail-on-error")

        with pytest.raises(GeneratorError, match="boom"):
            logger.error("boom")

    def test_error_formats_message_with_args(self) -> None:
        logger = FailOnErrorLogger("test-fail-on-error-format")

        with pytest.raises(GeneratorError, match="failed on node-1"):
            logger.error("failed on %s", "node-1")


class _Generator(BaseGenerator):
    async def generate(self, data: dict) -> None:
        return None


class TestBaseGeneratorLogger:
    def test_init_replaces_logger_class(self) -> None:
        def fake_init(self: object, *args: object, **kwargs: object) -> None:
            setattr(self, "logger", logging.getLogger("test-base-logger"))

        with patch.object(InfrahubGenerator, "__init__", fake_init):
            obj = _Generator()

        assert obj.logger.__class__ is FailOnErrorLogger
