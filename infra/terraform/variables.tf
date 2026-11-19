variable "aws_account_id" {
  description = "The 12-digit AWS account this stack may be applied to. Terraform refuses any other account, so a stale CLI login cannot receive resources."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-southeast-2"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "estatequery"
}

variable "instance_type" {
  description = "EC2 instance type. 2 GB of memory is comfortable for the app plus Caddy."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Size of the root disk in GB."
  type        = number
  default     = 20
}

variable "admin_cidr" {
  description = "CIDR allowed to SSH in. Empty means: the public IP of the machine running Terraform."
  type        = string
  default     = ""
}

variable "site_host" {
  description = "Public hostname for the site. Empty means <elastic-ip-with-dashes>.sslip.io, which needs no DNS account."
  type        = string
  default     = ""
}
