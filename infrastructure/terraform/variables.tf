variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (prod, staging)"
  type        = string
  default     = "prod"
}

variable "telegram_api_id" {
  description = "Telegram App API ID"
  type        = string
  sensitive   = true
}

variable "telegram_api_hash" {
  description = "Telegram App API Hash"
  type        = string
  sensitive   = true
}

variable "admin_user_id" {
  description = "Admin Telegram user ID"
  type        = string
  sensitive   = true
}

variable "payment_bot_token" {
  description = "Telegram payment bot token (from @BotFather)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "payment_bot_username" {
  description = "Telegram payment bot @username"
  type        = string
  default     = "KellyTributeBot"
}

variable "elevenlabs_api_key" {
  description = "ElevenLabs API key for voice synthesis"
  type        = string
  sensitive   = true
  default     = ""
}

variable "elevenlabs_voice_id" {
  description = "ElevenLabs voice clone ID"
  type        = string
  default     = ""
}

variable "monitor_auth_token" {
  description = "Auth token for monitoring dashboard"
  type        = string
  sensitive   = true
  default     = ""
}

variable "task_cpu" {
  description = "ECS task CPU units (256, 512, 1024, 2048, 4096)"
  type        = string
  default     = "512"
}

variable "task_memory" {
  description = "ECS task memory in MiB"
  type        = string
  default     = "1024"
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to access monitoring dashboard"
  type        = list(string)
  default     = ["0.0.0.0/0"]  # Restrict this to your IP in production!
}

variable "dashboard_domain" {
  description = "Domain for monitoring dashboard (e.g. dashboard.yourdomain.com)"
  type        = string
  default     = "dashboard.kellybot.example.com"
}

variable "alert_email" {
  description = "Email address for CloudWatch alarms"
  type        = string
  default     = ""
}
