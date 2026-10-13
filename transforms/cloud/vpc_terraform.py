"""Transform: CloudAccount → native HCL Terraform configuration (all VPCs in account)."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.cloud.aws import _account_blocks_aws, _provider_block_aws, _vpc_blocks_aws
from transforms.cloud.azure import _account_blocks_azure, _provider_block_azure, _vpc_blocks_azure
from transforms.cloud.gcp import _account_blocks_gcp, _provider_block_gcp, _vpc_blocks_gcp
from transforms.cloud.terraform_common import tf_id
from transforms.helpers.cloud import prepare_cloud_data
from utils.data_cleaning import clean_data


class CloudVpcTerraform(InfrahubTransform):
    """Generate native HCL Terraform for all VPCs in a CloudAccount."""

    query = "cloud_vpc_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        ctx = prepare_cloud_data(cleaned)
        if not ctx["vpcs"]:
            raise ValueError("No CloudVirtualNetwork found for this account")

        vpcs = ctx["vpcs"]
        provider_name = ctx["provider_name"]
        account = ctx["account"]
        lines: list[str] = []

        if provider_name == "aws":
            regions_seen = ctx["regions_seen"]
            vpc_region_map = ctx["vpc_region_map"]
            default_region = regions_seen[0] if regions_seen else ""
            lines += _provider_block_aws(regions_seen)

            for vpc in vpcs:
                region = vpc_region_map.get(vpc.get("name", ""), "")
                alias = tf_id(region) if region else ""
                provider_attr = f"aws.{alias}" if region != default_region and alias else ""
                lines += _vpc_blocks_aws(vpc, ctx, provider_attr=provider_attr)
            lines += _account_blocks_aws(ctx)  # TGW is account-wide; use default

        elif provider_name == "azure":
            account_name = account.get("name", "default")
            region_obj = vpcs[0].get("region") or {}
            location = region_obj.get("name", "eastus") if region_obj else "eastus"
            rg_ref = "var.resource_group_name"
            lines += _provider_block_azure(account_name)
            for vpc in vpcs:
                lines += _vpc_blocks_azure(vpc, ctx, rg_ref, location)
            lines += _account_blocks_azure(ctx, rg_ref, location)

        else:  # gcp
            project = account.get("account_id", "")
            region_obj = vpcs[0].get("region") or {}
            region = region_obj.get("name", "") if region_obj else ""
            lines += _provider_block_gcp(project, region)
            for vpc in vpcs:
                lines += _vpc_blocks_gcp(vpc, ctx, region)
            lines += _account_blocks_gcp(ctx, region)

        return "\n".join(lines)
