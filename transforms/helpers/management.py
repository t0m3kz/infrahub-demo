"""NTP, Syslog, SNMP, and AAA configuration helpers for device transforms."""

from typing import Any


def get_ntp(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract NTP configuration from device capabilities."""
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedNTP":
            continue
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
    return None


def get_syslog(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract Syslog configuration from device capabilities."""
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedSyslog":
            continue
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
    return None


def get_snmp(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract SNMP configuration from device capabilities."""
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedSNMP":
            continue
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
    return None


def get_aaa(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract AAA configuration from device capabilities."""
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedAAA":
            continue
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
    return None
