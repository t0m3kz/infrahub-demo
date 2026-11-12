"""Address space never reachable on the public internet (leaf ACL, SNAT check, edge transit filters).

IPv4 documentation ranges are absent on purpose: the demo uses them as stand-in public space.
"""

NON_INTERNET_PREFIXES: tuple[str, ...] = (
    "0.0.0.0/8",  # "this" network
    "10.0.0.0/8",  # RFC1918
    "100.64.0.0/10",  # RFC6598 shared space, covers the FW-Transit 100.66.x pools
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local
    "172.16.0.0/12",  # RFC1918
    "192.0.0.0/24",  # IETF protocol assignments
    "192.168.0.0/16",  # RFC1918
    "198.18.0.0/15",  # benchmarking, used for the demo partner extranet
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, incl. 255.255.255.255 broadcast
)

NON_INTERNET_PREFIXES_IPV6: tuple[str, ...] = (
    "::/128",  # unspecified
    "::1/128",  # loopback
    "::ffff:0:0/96",  # IPv4-mapped
    "64:ff9b::/96",  # NAT64 well-known prefix (translated IPv4 space)
    "100::/64",  # discard-only
    "2001:db8::/32",  # documentation
    "fc00::/7",  # unique local (ULA), covers the tenant pools
    "fe80::/10",  # link-local
    "ff00::/8",  # multicast
)
"""IPv6 space that is not the public internet (IANA special-purpose registry); documentation space 2001:db8::/32 is
included.
"""


def non_internet_prefixes(family: str) -> tuple[str, ...]:
    """The non-internet prefixes of an address family (``ipv4`` or ``ipv6``)."""
    return NON_INTERNET_PREFIXES_IPV6 if family == "ipv6" else NON_INTERNET_PREFIXES
