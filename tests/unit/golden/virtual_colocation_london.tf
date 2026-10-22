terraform {
  required_providers {
    equinix = {
      source = "equinix/equinix"
    }
  }
}

variable "ld_ne_fw_ld_node0_license_token" {
  type      = string
  sensitive = true
}

variable "ld_ne_fw_ld_node1_license_token" {
  type      = string
  sensitive = true
}

resource "equinix_network_device" "ld_ne_eg_ld01" {
  name           = "eg-ld01"
  metro_code     = "LD"
  type_code      = "ARISTA-ROUTER"
  package_code   = "CloudEOS"
  version        = "4.29.0"
  core_count     = 4
  term_length    = 12
  account_number = "1000001"
  notifications  = ["noc@example.com", "network-ops@example.com"]
  secondary_device {
    name           = "eg-ld02"
    metro_code     = "LD"
    account_number = "1000001"
    notifications  = ["noc@example.com", "network-ops@example.com"]
  }
}

resource "equinix_network_device" "ld_ne_fw_ld" {
  name           = "fw-ld"
  metro_code     = "LD"
  type_code      = "PA-VM"
  package_code   = "VM100"
  version        = "10.1.3"
  core_count     = 2
  term_length    = 12
  account_number = "1000001"
  notifications  = ["noc@example.com", "network-ops@example.com"]
  byol           = true
  cluster_details {
    cluster_name = "fw-ld-cluster"
    node0 {
      vendor_configuration {
        hostname = "fw-ld01"
      }
      license_token = var.ld_ne_fw_ld_node0_license_token
    }
    node1 {
      vendor_configuration {
        hostname = "fw-ld02"
      }
      license_token = var.ld_ne_fw_ld_node1_license_token
    }
  }
}

resource "equinix_network_device" "ld_ne_sdwan_ld01" {
  name           = "sdwan-ld01"
  metro_code     = "LD"
  type_code      = "FG-SDWAN"
  package_code   = "VM02"
  version        = "7.0.14"
  core_count     = 2
  term_length    = 12
  account_number = "1000001"
  notifications  = ["noc@example.com", "network-ops@example.com"]
  diverse_device_id = equinix_network_device.ld_ne_eg_ld01.uuid
}
