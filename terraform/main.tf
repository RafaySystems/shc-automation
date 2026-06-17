################################################################################
# Rafay Controller — OCI Instance Provisioning
# tfvars auto-generated from dev.yaml by lib/terraform/tf_manager.py
# DNS is managed by Python/boto3 in conftest.py (not Terraform)
# HA mode: creates 3 instances + 3 data volumes
################################################################################

terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
}

provider "oci" {
  config_file_profile = var.oci_profile
}

# ── Data volumes (1TB each) ───────────────────────────────────────────────────
resource "oci_core_volume" "data_volume" {
  count               = var.ha ? 3 : 1
  compartment_id      = var.compartment_id
  availability_domain = var.availability_domain
  display_name        = "${var.display_name}-data-${count.index + 1}"
  size_in_gbs         = var.data_volume_gb
  freeform_tags       = var.tags
  lifecycle { ignore_changes = [defined_tags] }
}

# ── Compute instances ─────────────────────────────────────────────────────────
resource "oci_core_instance" "controller" {
  count               = var.ha ? 3 : 1
  compartment_id      = var.compartment_id
  availability_domain = var.availability_domain
  display_name        = var.ha ? "${var.display_name}-node${count.index + 1}" : var.display_name
  shape               = var.shape

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  source_details {
    source_type             = "image"
    source_id               = var.image_id
    boot_volume_size_in_gbs = var.boot_volume_gb
  }

  create_vnic_details {
    subnet_id        = var.subnet_id
    assign_public_ip = true
    display_name     = var.ha ? "${var.display_name}-node${count.index + 1}-vnic" : "${var.display_name}-vnic"
  }

  metadata = {
    ssh_authorized_keys = file(var.ssh_public_key_path)
  }

  freeform_tags = var.tags
  lifecycle { ignore_changes = [defined_tags] }
  timeouts { create = "20m" }
}

# ── Attach data volumes ───────────────────────────────────────────────────────
resource "oci_core_volume_attachment" "data_attachment" {
  count           = var.ha ? 3 : 1
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.controller[count.index].id
  volume_id       = oci_core_volume.data_volume[count.index].id
  display_name    = var.ha ? "${var.display_name}-data-attachment-${count.index + 1}" : "${var.display_name}-data-attachment"
  is_read_only    = false
  depends_on      = [oci_core_instance.controller]
  timeouts { create = "10m" }
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "instance_ids" {
  value = oci_core_instance.controller[*].id
}

output "public_ips" {
  value = oci_core_instance.controller[*].public_ip
}

output "display_names" {
  value = oci_core_instance.controller[*].display_name
}

output "data_volume_ids" {
  value = oci_core_volume.data_volume[*].id
}

# Convenience outputs for non-HA (single node)
output "instance_id" {
  value = oci_core_instance.controller[0].id
}

output "public_ip" {
  value = oci_core_instance.controller[0].public_ip
}
