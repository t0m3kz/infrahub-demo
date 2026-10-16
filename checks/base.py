from infrahub_sdk.checks import InfrahubCheck


class WarningCheck(InfrahubCheck):
    """InfrahubCheck with a warning level.

    The SDK has only error and info: an error blocks the merge, info does not.
    A warning is an info line prefixed so it stands out in the check log.
    """

    def log_warning(self, message: str) -> None:
        self.log_info(message=f"WARNING: {message}")
