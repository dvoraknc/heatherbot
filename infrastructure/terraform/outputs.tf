output "ecr_repository_url" {
  description = "ECR repository URL for pushing Docker images"
  value       = aws_ecr_repository.bot.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.bot.name
}

output "secrets_manager_arn" {
  description = "ARN of the Secrets Manager secret"
  value       = aws_secretsmanager_secret.bot_secrets.arn
}

output "s3_media_bucket" {
  description = "S3 bucket for media assets"
  value       = aws_s3_bucket.media.id
}

output "alb_dns_name" {
  description = "ALB DNS name for monitoring dashboard"
  value       = aws_lb.main.dns_name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for bot logs"
  value       = aws_cloudwatch_log_group.bot.name
}

output "kms_key_id" {
  description = "KMS key ID for encryption"
  value       = aws_kms_key.main.key_id
  sensitive   = true
}
