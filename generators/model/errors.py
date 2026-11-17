"""The one error a model compiler raises for a payload it cannot build a fabric from."""


class FabricModelError(ValueError):
    """A payload that violates its design envelope; the message is the text the generator logs."""


class GeneratorError(Exception):
    """Raised when a generator logs an error, which fails the Infrahub task."""
