"""Validate firewall."""

from infrahub_sdk.checks import InfrahubCheck


class InfrahubValidateFirewall(InfrahubCheck):
    """Check Firewall."""

    query = "validate_firewall"

    def validate(self, data):
        """Validate firewall."""
        for interface in data["InfraDevice"]["edges"][0]["node"]["interfaces"][
            "edges"
        ]:
            # Validate if security zone is set
            if (
                interface["node"]["role"]["value"] != "management"
                and interface["node"]["security_zone"]["node"] is None
            ):
                _int = interface["node"]["name"]["value"]
                self.log_error(
                    message=f"No security zone assigned to interface {_int}."
                )
