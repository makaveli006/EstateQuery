#!/bin/bash
# First-boot setup for the host: Docker, the Compose plugin and the app directory.
set -euxo pipefail

dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user

# Amazon Linux does not package the Compose plugin; install the official release binary.
install -d /usr/local/lib/docker/cli-plugins
curl -fsSL --retry 5 --retry-delay 5 \
  "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

install -d -o ec2-user -g ec2-user /opt/estatequery
