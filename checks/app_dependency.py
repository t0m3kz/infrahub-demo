"""Validate AppDependency source/target shape and ports."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.ports import PortProfileHelper, PortSpec


class CheckAppDependency(InfrahubCheck):
    """The schema cannot say "exactly one of", so this check does.

    A dependency the generator cannot turn into a rule is skipped there with
    only a log line; reporting it here makes the missing flow visible on the
    proposed change instead.
    """

    query = "app_dependency_validation"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for component in cleaned.get("AppComponent") or []:
            self._parse_ports(component.get("ports"), f"Component '{component.get('fqdn', '<unnamed>')}'")
        for dep in cleaned.get("AppDependency") or []:
            self._validate_dependency(dep)

    def _validate_dependency(self, dep: dict[str, Any]) -> None:
        label = f"Dependency '{dep.get('name', '<unnamed>')}'"
        source = dep.get("source") or {}
        target = dep.get("target") or {}
        target_fqdn = str(dep.get("target_fqdn") or "").strip()

        if bool(source) == bool(dep.get("source_profile")):
            self.log_error(message=f"{label} must set exactly one of source or source_profile.")
        if bool(target) == bool(target_fqdn):
            self.log_error(message=f"{label} must set exactly one of target or target_fqdn.")
            return

        ports = self._parse_ports(dep.get("ports"), label)
        if ports is None:
            return

        if target_fqdn:
            if not ports:
                self.log_error(message=f"{label} targets {target_fqdn} and must list its ports.")
            if dep.get("source_profile"):
                self.log_error(message=f"{label} grants an access profile an external fqdn; grants target a component.")
            elif source:
                owner = ((source.get("parent") or {}).get("owner")) or {}
                if not owner.get("egress_service"):
                    self.log_error(
                        message=(
                            f"{label} targets {target_fqdn}, but owner '{owner.get('name', '<unknown>')}' has no "
                            "egress_service to reach it through."
                        )
                    )
            return

        # The component loop already reported a target port that does not parse.
        target_ports = self._parse_ports(target.get("ports"), "", report=False)
        if target_ports is None:
            return
        if not target_ports:
            if not ports:
                self.log_error(message=f"{label} lists no ports and its target '{target.get('fqdn')}' declares none.")
            return
        for port in ports:
            if not any(self._covers(listened, port) for listened in target_ports):
                self.log_error(
                    message=(
                        f"{label} opens {PortProfileHelper.format_port_spec(port)}, which target "
                        f"'{target.get('fqdn')}' does not list in its ports."
                    )
                )

    def _parse_ports(self, specs: list[str] | None, label: str, report: bool = True) -> list[PortSpec] | None:
        """Parsed ports, or None once a port does not parse (logging the first one when report is set)."""
        ports: list[PortSpec] = []
        for spec in specs or []:
            try:
                ports.append(PortProfileHelper.parse_port_spec(spec))
            except ValueError as exc:
                if report:
                    self.log_error(message=f"{label}: {exc}.")
                return None
        return ports

    @staticmethod
    def _covers(listened: PortSpec, port: PortSpec) -> bool:
        protocol, start, end = port
        l_protocol, l_start, l_end = listened
        return protocol == l_protocol and l_start <= start and (end or start) <= (l_end or l_start)
