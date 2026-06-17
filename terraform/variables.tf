################################################################################
# Variables — injected via auto-generated terraform.tfvars (from dev.yaml)
# DO NOT EDIT — source of truth is dev.yaml
################################################################################

variable "oci_profile" {
  type    = string
  default = "DEFAULT"
}

variable "compartment_id" {
  type = string
}

variable "availability_domain" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "image_id" {
  type = string
}

variable "shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
}

variable "ocpus" {
  type    = number
  default = 16
}

variable "memory_gb" {
  type    = number
  default = 64
}

variable "boot_volume_gb" {
  type    = number
  default = 500
}

variable "data_volume_gb" {
  type    = number
  default = 1024
}

variable "ssh_public_key_path" {
  type = string
}

variable "display_name" {
  type    = string
  default = "rafay-controller"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "ha" {
  description = "HA mode — creates 3 instances when true, 1 when false"
  type        = bool
  default     = false
}
