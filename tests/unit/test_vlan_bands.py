"""The VLAN bands a border leaf's service-port trunk and VLAN table draw from must not overlap."""

from generators.helpers.pools import (
    CUSTOMER_VLAN_ID_MAX,
    CUSTOMER_VLAN_ID_MIN,
    FW_CONTEXT_VLAN_END,
    FW_CONTEXT_VLAN_START,
)
from transforms.helpers.vxlan import _L3VNI_SVI_VLAN_BASE


def test_customer_and_firewall_context_vlans_are_disjoint() -> None:
    """get_vlans de-duplicates by VLAN ID, so a customer VLAN inside the context band would vanish silently."""
    assert CUSTOMER_VLAN_ID_MIN < CUSTOMER_VLAN_ID_MAX < FW_CONTEXT_VLAN_START <= FW_CONTEXT_VLAN_END


def test_firewall_context_band_leaves_room_below_the_l3vni_svi_band() -> None:
    """Per-VRF transit VLANs are the context VLAN plus up to 3 x 200, and must stay under the L3 VNI SVI band."""
    assert FW_CONTEXT_VLAN_END + 3 * 200 < _L3VNI_SVI_VLAN_BASE
