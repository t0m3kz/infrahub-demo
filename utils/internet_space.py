"""Address space that is never reachable on the public internet.

The one list shared by every reader that has to tell "internet" from "internal":
the leaf ACL (a permit to the internet zone is preceded by a
deny for each of these, transforms/helpers/acl.py), the SNAT-address check
(utils/exchange_transit.py is_public_address: a firewall context's SNAT address
must lie outside this space) and the edge transit filters. Documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24)
are deliberately absent: the demo uses them as stand-in public space.
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
