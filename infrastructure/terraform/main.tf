# =============================================================================
# Terraform — KellyFindomBot AWS Infrastructure
# =============================================================================
# Resources created:
#   - VPC with public/private subnets
#   - ECR repository (Docker images)
#   - ECS Fargate cluster + service + task definition
#   - AWS Secrets Manager (all bot credentials)
#   - S3 (media library, user profile backups)
#   - CloudWatch (logs + alarms)
#   - IAM roles with least-privilege policies
#   - Application Load Balancer (monitoring dashboard)
#
# Prerequisites:
#   terraform init
#   terraform plan -var-file=terraform.tfvars
#   terraform apply -var-file=terraform.tfvars
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }

  # Remote state — bucket is passed via -backend-config="bucket=..." at init time
  # Run bootstrap.sh (local) or the terraform.yml GitHub Actions workflow to init.
  backend "s3" {
    key     = "prod/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
    # bucket = injected via -backend-config flag
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "KellyFindomBot"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# =============================================================================
# DATA SOURCES
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  name_prefix = "kelly-${var.environment}"
}

# =============================================================================
# NETWORKING
# =============================================================================

resource "aws_vpc" "main" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags = { Name = "${local.name_prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.${count.index + 10}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags = { Name = "${local.name_prefix}-private-${count.index}" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
  tags          = { Name = "${local.name_prefix}-nat" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name_prefix}-public-rt" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name_prefix}-private-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# =============================================================================
# SECURITY GROUPS
# =============================================================================

resource "aws_security_group" "bot" {
  name        = "${local.name_prefix}-bot-sg"
  description = "Kelly bot container security group"
  vpc_id      = aws_vpc.main.id

  # Allow outbound to Telegram API, ElevenLabs, llama-server
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound (Telegram, ElevenLabs, LLM APIs)"
  }

  # Monitoring dashboard — from ALB only
  ingress {
    from_port       = 8888
    to_port         = 8888
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
    description     = "Monitoring dashboard from ALB"
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "ALB security group"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
    description = "HTTP from allowed IPs"
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
    description = "HTTPS from allowed IPs only"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# =============================================================================
# ECR — CONTAINER REGISTRY
# =============================================================================

resource "aws_ecr_repository" "bot" {
  name                 = "${local.name_prefix}-bot"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

}

resource "aws_ecr_lifecycle_policy" "bot" {
  repository = aws_ecr_repository.bot.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# =============================================================================
# KMS KEY
# =============================================================================

resource "aws_kms_key" "main" {
  description             = "KellyFindomBot encryption key"
  deletion_window_in_days = 7
  enable_key_rotation     = false
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.main.key_id
}

# =============================================================================
# AWS SECRETS MANAGER
# =============================================================================

resource "aws_secretsmanager_secret" "bot_secrets" {
  name        = "kellyfindombot/${var.environment}/secrets"
  description = "KellyFindomBot all credentials"

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "bot_secrets" {
  secret_id = aws_secretsmanager_secret.bot_secrets.id
  secret_string = jsonencode({
    # Telegram — filled by bootstrap.sh
    telegram_api_id    = var.telegram_api_id
    telegram_api_hash  = var.telegram_api_hash
    admin_user_id      = var.admin_user_id
    payment_bot_token  = var.payment_bot_token
    payment_bot_username = var.payment_bot_username

    # ElevenLabs voice
    elevenlabs_api_key  = var.elevenlabs_api_key
    elevenlabs_voice_id = var.elevenlabs_voice_id

    # Monitoring
    monitor_auth_token = var.monitor_auth_token

    # LLM endpoints (set these after deploying your LLM service)
    text_ai_port  = "1234"
    image_ai_port = "11434"
    tts_port      = "5001"
  })

  lifecycle {
    ignore_changes = [secret_string]  # Allow manual updates without TF overwrite
  }
}

# =============================================================================
# S3 — MEDIA & BACKUPS
# =============================================================================

resource "aws_s3_bucket" "media" {
  bucket = "${local.name_prefix}-media-${local.account_id}"
}

resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "tfstate" {
  bucket = "${local.name_prefix}-tfstate-${local.account_id}"
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
# IAM — ECS TASK ROLE
# =============================================================================

resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task" {
  name = "${local.name_prefix}-ecs-task-policy"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [aws_secretsmanager_secret.bot_secrets.arn]
      },
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.main.arn]
      },
      {
        Sid    = "S3MediaAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.media.arn,
          "${aws_s3_bucket.media.arn}/*"
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/ecs/${local.name_prefix}*"]
      }
    ]
  })
}

# =============================================================================
# ECS — FARGATE CLUSTER & SERVICE
# =============================================================================

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/ecs/${local.name_prefix}/bot"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "bot" {
  family                   = "${local.name_prefix}-bot"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "kelly-bot"
    image     = "${aws_ecr_repository.bot.repository_url}:latest"
    essential = true

    environment = [
      { name = "USE_AWS_SECRETS", value = "true" },
      { name = "KELLY_SECRET_NAME", value = aws_secretsmanager_secret.bot_secrets.name },
      { name = "AWS_REGION", value = local.region },
      { name = "S3_MEDIA_BUCKET", value = aws_s3_bucket.media.id },
      { name = "S3_SESSION_KEY", value = "session/kelly_session.session" },
      { name = "TELEGRAM_SESSION_FILE", value = "kelly_session.session" },
      { name = "BOT_PERSONA", value = "kelly" },
      { name = "ENABLE_MONETIZATION", value = "true" },
      { name = "LOG_LEVEL", value = "INFO" },
    ]

    portMappings = [
      { containerPort = 8888, protocol = "tcp", name = "dashboard" }
    ]

    mountPoints = [
      { sourceVolume = "user-profiles", containerPath = "/app/user_profiles" },
      { sourceVolume = "bot-logs",      containerPath = "/app/logs" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.bot.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8888/health', timeout=5)\" || exit 1"]
      interval    = 60
      timeout     = 10
      retries     = 3
      startPeriod = 60
    }
  }])

  volume {
    name = "user-profiles"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.profiles.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.profiles.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "bot-logs"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.profiles.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.logs.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "bot" {
  name            = "${local.name_prefix}-bot"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.bot.arn
  launch_type     = "FARGATE"
  desired_count   = 1

  # Prevent service replacement during deploy — ensures only 1 instance
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.bot.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.dashboard.arn
    container_name   = "kelly-bot"
    container_port   = 8888
  }

  depends_on = [aws_lb_listener.https]

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# =============================================================================
# EFS — PERSISTENT STORAGE (user profiles, logs)
# =============================================================================

resource "aws_efs_file_system" "profiles" {
  creation_token  = "${local.name_prefix}-profiles"
  encrypted       = true
  throughput_mode = "bursting"
}

resource "aws_efs_access_point" "profiles" {
  file_system_id = aws_efs_file_system.profiles.id
  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/profiles"

    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }
}

resource "aws_efs_access_point" "logs" {
  file_system_id = aws_efs_file_system.profiles.id
  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/logs"

    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }
}

resource "aws_efs_mount_target" "profiles" {
  count           = 2
  file_system_id  = aws_efs_file_system.profiles.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_security_group" "efs" {
  name        = "${local.name_prefix}-efs-sg"
  description = "EFS mount target security group"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.bot.id]
    description     = "NFS from ECS tasks"
  }
}

# =============================================================================
# ALB — MONITORING DASHBOARD
# =============================================================================

resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  enable_deletion_protection = false
}

resource "aws_lb_target_group" "dashboard" {
  name        = "${local.name_prefix}-dashboard"
  port        = 8888
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    interval            = 60
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard.arn
  }
}

# =============================================================================
# CLOUDWATCH ALARMS
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "bot_cpu" {
  alarm_name          = "${local.name_prefix}-high-cpu"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 120
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Kelly bot CPU above 80%"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.bot.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "bot_memory" {
  alarm_name          = "${local.name_prefix}-high-memory"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 120
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "Kelly bot memory above 85%"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.bot.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# =============================================================================
# GITHUB ACTIONS — OIDC PROVIDER & DEPLOY ROLE
# =============================================================================

resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # GitHub's OIDC CA thumbprint — verified 2024-01.
  # Note: AWS validates GitHub OIDC tokens server-side; this thumbprint is a
  # belt-and-suspenders check. Update if GitHub rotates their OIDC CA.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions" {
  name        = "kelly-github-actions"
  description = "Assumed by GitHub Actions via OIDC to build and deploy the bot"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github_actions.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only the main branch can assume this role — prevents deployments from forks/PRs
          "token.actions.githubusercontent.com:sub" = "repo:ranoli90/KellyFindomBot:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  name = "kelly-github-actions-deploy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage"
        ]
        Resource = [aws_ecr_repository.bot.arn]
      },
      {
        Sid    = "ECSUpdate"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition"
        ]
        Resource = [
          aws_ecs_cluster.main.arn,
          "arn:aws:ecs:${local.region}:${local.account_id}:service/${aws_ecs_cluster.main.name}/${aws_ecs_service.bot.name}"
        ]
      }
    ]
  })
}
