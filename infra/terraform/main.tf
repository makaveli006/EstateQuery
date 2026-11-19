# EstateQuery infrastructure: one small EC2 host with a fixed public IP.
#
# The application itself is a Docker Compose stack (app + Caddy). Terraform creates the
# machine it runs on; deploy/deploy.sh ships the image to it. Keeping the two apart means
# a new app version never touches the infrastructure state.

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}

# --- lookups -------------------------------------------------------------------

# Amazon Linux 2023: its package repositories are served from AWS, so first-boot setup
# does not depend on any third-party mirror being reachable.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

data "http" "my_ip" {
  url = "https://checkip.amazonaws.com"
}

locals {
  admin_cidr = var.admin_cidr != "" ? var.admin_cidr : "${chomp(data.http.my_ip.response_body)}/32"
  site_host  = var.site_host != "" ? var.site_host : "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
}

# --- SSH key: generated here, private half written to a git-ignored file ---------

resource "tls_private_key" "deploy" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "deploy" {
  key_name   = "${var.name}-deploy"
  public_key = tls_private_key.deploy.public_key_openssh
}

resource "local_sensitive_file" "private_key" {
  content         = tls_private_key.deploy.private_key_openssh
  filename        = "${path.module}/.secrets/${var.name}.pem"
  file_permission = "0600"
}

# --- network: web open to everyone, SSH only from the admin address -----------------

resource "aws_security_group" "web" {
  name        = "${var.name}-web"
  description = "EstateQuery: HTTP/HTTPS from anywhere, SSH from the admin address"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP (redirects to HTTPS, certificate challenges)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH for deployment"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.admin_cidr]
  }

  egress {
    description = "Outbound to model APIs, Langfuse and package repositories"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name}-web"
  }
}

# --- the host ------------------------------------------------------------------------

resource "aws_instance" "app" {
  ami                         = nonsensitive(data.aws_ssm_parameter.al2023_ami.value)
  instance_type               = var.instance_type
  key_name                    = aws_key_pair.deploy.key_name
  vpc_security_group_ids      = [aws_security_group.web.id]
  user_data                   = file("${path.module}/user_data.sh")
  user_data_replace_on_change = true

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = {
    Name = var.name
  }

  lifecycle {
    # A newer AMI being published must not replace a running server on the next apply.
    ignore_changes = [ami]
  }
}

# A fixed address, so the hostname and its TLS certificate survive stop/start.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = var.name
  }
}
