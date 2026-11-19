output "public_ip" {
  description = "Elastic IP of the host."
  value       = aws_eip.app.public_ip
}

output "site_host" {
  description = "Hostname Caddy requests a TLS certificate for."
  value       = local.site_host
}

output "url" {
  description = "Public URL of the chatbot once deploy/deploy.sh has run."
  value       = "https://${local.site_host}"
}

output "ssh_user" {
  description = "Login user of the Amazon Linux image."
  value       = "ec2-user"
}

output "ssh_key_path" {
  description = "Path of the generated private key (git-ignored)."
  value       = abspath(local_sensitive_file.private_key.filename)
}

output "instance_id" {
  value = aws_instance.app.id
}
