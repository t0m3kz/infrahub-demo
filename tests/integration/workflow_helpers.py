"""The kick load function of the integration suite."""

import logging
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)


def object_loader(
    execute_command: Callable[..., Any], address: str, **options: int
) -> Callable[[Sequence[str], str], bool]:
    """A kick load function (tasks.run_kick) that runs `infrahubctl object load` against the test stack."""

    def _load(paths: Sequence[str], branch: str) -> bool:
        result = execute_command(
            f"infrahubctl object load {' '.join(paths)} --branch {branch}", address=address, **options
        )
        if result.returncode != 0:
            logger.error("Load of %s failed:\n%s\n%s", paths, result.stdout, result.stderr)
        return result.returncode == 0

    return _load
