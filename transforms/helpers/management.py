"""NTP, Syslog, SNMP, and AAA configuration helpers for device transforms."""

from typing import Any


def _capabilities_by_typename(device_capabilities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index capabilities by typename, keeping the first instance per type."""
    indexed: dict[str, dict[str, Any]] = {}
    for service in device_capabilities or []:
        typename = service.get("typename")
        if typename and typename not in indexed:
            indexed[typename] = service
    return indexed


def get_management_services(device_capabilities: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Extract all management services in one pass over capabilities."""
    indexed = _capabilities_by_typename(device_capabilities)
    return {
        "ntp": _ntp_from_service(indexed.get("ManagedNTP")),
        "syslog": _syslog_from_service(indexed.get("ManagedSyslog")),
        "snmp": _snmp_from_service(indexed.get("ManagedSNMP")),
        "aaa": _aaa_from_service(indexed.get("ManagedAAA")),
    }


def _ntp_from_service(service: dict[str, Any] | None) -> dict[str, Any] | None:
    if not service:
        return None
    servers = [
        {
            "address": s.get("address"),
            "prefer": s.get("prefer", False),
            "version": s.get("version", 4),
        }
        for s in (service.get("servers") or [])
        if s.get("address")
    ]
    return {
        "timezone": service.get("timezone", "UTC"),
        "servers": servers,
    }


def _syslog_from_service(service: dict[str, Any] | None) -> dict[str, Any] | None:
    if not service:
        return None
    servers = [
        {
            "address": s.get("address"),
            "port": s.get("port", 514),
            "severity": s.get("severity", "informational"),
        }
        for s in (service.get("servers") or [])
        if s.get("address")
    ]
    return {"servers": servers}


def _snmp_from_service(service: dict[str, Any] | None) -> dict[str, Any] | None:
    if not service:
        return None
    trap_targets = [
        {
            "address": t.get("address"),
            "port": t.get("port", 162),
            "community": t.get("community"),
        }
        for t in (service.get("trap_targets") or [])
        if t.get("address")
    ]
    return {
        "version": service.get("version", "v2c"),
        "community_ro": service.get("community_ro"),
        "community_rw": service.get("community_rw"),
        "location": service.get("location"),
        "contact": service.get("contact"),
        "trap_targets": trap_targets,
    }


def _aaa_from_service(service: dict[str, Any] | None) -> dict[str, Any] | None:
    if not service:
        return None
    servers = [
        {
            "address": s.get("address"),
            "protocol": s.get("protocol", "tacacs"),
            "port": s.get("port"),
            "timeout": s.get("timeout", 5),
        }
        for s in (service.get("servers") or [])
        if s.get("address")
    ]
    return {
        "authentication_order": service.get("authentication_order", "tacacs_local"),
        "authorization_commands": service.get("authorization_commands", False),
        "accounting_enabled": service.get("accounting_enabled", False),
        "servers": servers,
    }


def get_ntp(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract NTP configuration from device capabilities."""
    return _ntp_from_service(_capabilities_by_typename(device_capabilities).get("ManagedNTP"))


def get_syslog(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract Syslog configuration from device capabilities."""
    return _syslog_from_service(_capabilities_by_typename(device_capabilities).get("ManagedSyslog"))


def get_snmp(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract SNMP configuration from device capabilities."""
    return _snmp_from_service(_capabilities_by_typename(device_capabilities).get("ManagedSNMP"))


def get_aaa(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract AAA configuration from device capabilities."""
    return _aaa_from_service(_capabilities_by_typename(device_capabilities).get("ManagedAAA"))
