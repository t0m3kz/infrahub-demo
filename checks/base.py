from infrahub_sdk.checks import InfrahubCheck


class WarningCheck(InfrahubCheck):
    """InfrahubCheck with a warning level: an info line prefixed so it stands out."""

    def log_warning(self, message: str) -> None:
        self.log_info(message=f"WARNING: {message}")
