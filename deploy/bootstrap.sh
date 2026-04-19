#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — KellyFindomBot AWS One-Shot Setup
# =============================================================================
# Run this ONCE from your local machine to:
#   1. Verify AWS credentials
#   2. Seed all secrets into AWS Secrets Manager
#   3. Create Terraform state S3 bucket (if it doesn't exist)
#   4. Run terraform init + apply
#   5. Build & push Docker image to ECR
#   6. Force a new ECS deployment
#
# Prerequisites:
#   - AWS CLI configured (aws configure OR aws-vault)
#   - Docker installed and running
#   - Terraform >= 1.5 installed
#
# Usage:
#   chmod +x deploy/bootstrap.sh
#   ./deploy/bootstrap.sh
#
# IMPORTANT: Never commit the terraform.tfvars file — it contains secrets.
# It is already in .gitignore.
# =============================================================================

set -euo pipefail

# ─── Color helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*" >&2; exit 1; }

# ─── Configuration ────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
    || error "AWS credentials not configured. Run: aws configure"
PROJECT="kelly-prod"
SECRET_NAME="kellyfindombot/prod/secrets"
TFSTATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_ID}"
ECR_REPO="${PROJECT}-bot"
CLUSTER="${PROJECT}-cluster"
SERVICE="${PROJECT}-bot"
IMAGE_TAG=$(git rev-parse --short HEAD 2>/dev/null || echo "latest")

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  KellyFindomBot — AWS Bootstrap"
echo "  Account : ${ACCOUNT_ID}"
echo "  Region  : ${AWS_REGION}"
echo "  Image   : ${IMAGE_TAG}"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ─── Step 1: Collect secrets interactively ────────────────────────────────────
info "Step 1: Collecting secrets..."
echo ""

prompt_secret() {
    local name="$1"; local prompt="$2"; local val=""
    read -rsp "  ${prompt}: " val; echo ""
    echo "$val"
}

TELEGRAM_API_ID=$(prompt_secret "telegram_api_id" "Telegram API ID")
TELEGRAM_API_HASH=$(prompt_secret "telegram_api_hash" "Telegram API Hash")
ADMIN_USER_ID=$(prompt_secret "admin_user_id" "Admin Telegram User ID")
PAYMENT_BOT_TOKEN=$(prompt_secret "payment_bot_token" "Payment Bot Token (leave blank to skip)")
PAYMENT_BOT_USERNAME="${PAYMENT_BOT_USERNAME:-KellyTributeBot}"
ELEVENLABS_API_KEY=$(prompt_secret "elevenlabs_api_key" "ElevenLabs API Key (leave blank to skip)")
ELEVENLABS_VOICE_ID="${ELEVENLABS_VOICE_ID:-}"
MONITOR_AUTH_TOKEN=$(openssl rand -hex 32)

echo ""
success "Secrets collected (will be stored in AWS Secrets Manager only — never in code)"

# ─── Step 2: Create/update Secrets Manager ────────────────────────────────────
info "Step 2: Seeding AWS Secrets Manager (${SECRET_NAME})..."

SECRET_JSON=$(cat <<EOF
{
  "telegram_api_id":      "${TELEGRAM_API_ID}",
  "telegram_api_hash":    "${TELEGRAM_API_HASH}",
  "admin_user_id":        "${ADMIN_USER_ID}",
  "payment_bot_token":    "${PAYMENT_BOT_TOKEN}",
  "payment_bot_username": "${PAYMENT_BOT_USERNAME}",
  "elevenlabs_api_key":   "${ELEVENLABS_API_KEY}",
  "elevenlabs_voice_id":  "${ELEVENLABS_VOICE_ID}",
  "monitor_auth_token":   "${MONITOR_AUTH_TOKEN}"
}
EOF
)

# Try update first, create if doesn't exist
if aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" --region "${AWS_REGION}" &>/dev/null; then
    aws secretsmanager put-secret-value \
        --secret-id "${SECRET_NAME}" \
        --secret-string "${SECRET_JSON}" \
        --region "${AWS_REGION}" > /dev/null
    success "Updated existing secret"
else
    aws secretsmanager create-secret \
        --name "${SECRET_NAME}" \
        --description "KellyFindomBot production credentials" \
        --secret-string "${SECRET_JSON}" \
        --region "${AWS_REGION}" > /dev/null
    success "Created new secret"
fi

# Clear from shell memory immediately
unset TELEGRAM_API_ID TELEGRAM_API_HASH ADMIN_USER_ID PAYMENT_BOT_TOKEN ELEVENLABS_API_KEY SECRET_JSON

# ─── Step 3: Terraform state bucket ───────────────────────────────────────────
info "Step 3: Ensuring Terraform state bucket (${TFSTATE_BUCKET})..."

if ! aws s3api head-bucket --bucket "${TFSTATE_BUCKET}" --region "${AWS_REGION}" 2>/dev/null; then
    aws s3api create-bucket \
        --bucket "${TFSTATE_BUCKET}" \
        --region "${AWS_REGION}" \
        $([ "${AWS_REGION}" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=${AWS_REGION}") \
        > /dev/null
    aws s3api put-bucket-versioning \
        --bucket "${TFSTATE_BUCKET}" \
        --versioning-configuration Status=Enabled \
        > /dev/null
    aws s3api put-bucket-encryption \
        --bucket "${TFSTATE_BUCKET}" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
        > /dev/null
    aws s3api put-public-access-block \
        --bucket "${TFSTATE_BUCKET}" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
        > /dev/null
    success "Created state bucket: ${TFSTATE_BUCKET}"
else
    success "State bucket already exists"
fi

# ─── Step 4: Write terraform.tfvars (secrets fetched from SM at apply time) ───
info "Step 4: Writing terraform.tfvars..."

cat > infrastructure/terraform/terraform.tfvars <<EOF
# Auto-generated by bootstrap.sh — DO NOT COMMIT
# Secrets are read from AWS Secrets Manager at runtime.
# Only non-secret config goes here.
aws_region          = "${AWS_REGION}"
environment         = "prod"
telegram_api_id     = "LOADED_FROM_SECRETS_MANAGER"
telegram_api_hash   = "LOADED_FROM_SECRETS_MANAGER"
admin_user_id       = "LOADED_FROM_SECRETS_MANAGER"
task_cpu            = "512"
task_memory         = "1024"
allowed_cidr_blocks = ["0.0.0.0/0"]
alert_email         = ""
EOF

success "terraform.tfvars written"

# ─── Step 5: Terraform init + apply ───────────────────────────────────────────
info "Step 5: Running Terraform..."

cd infrastructure/terraform

# Enable remote state (uncomment backend after bucket exists)
terraform init -upgrade -backend-config="bucket=${TFSTATE_BUCKET}"
terraform plan -out=tfplan -compact-warnings
echo ""
warn "Review the plan above. Press Enter to apply or Ctrl+C to abort."
read -r

terraform apply tfplan
success "Terraform apply complete"

# Grab outputs
ECR_URL=$(terraform output -raw ecr_repository_url)
CLUSTER_NAME=$(terraform output -raw ecs_cluster_name)
SERVICE_NAME=$(terraform output -raw ecs_service_name)

cd ../..

# ─── Step 6: Build & push Docker image ────────────────────────────────────────
info "Step 6: Building and pushing Docker image..."

aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_URL}"

docker build -t "${ECR_URL}:${IMAGE_TAG}" -t "${ECR_URL}:latest" .
docker push "${ECR_URL}:${IMAGE_TAG}"
docker push "${ECR_URL}:latest"

success "Image pushed: ${ECR_URL}:${IMAGE_TAG}"

# ─── Step 7: Deploy new revision ──────────────────────────────────────────────
info "Step 7: Deploying new ECS revision..."

aws ecs update-service \
    --cluster "${CLUSTER_NAME}" \
    --service "${SERVICE_NAME}" \
    --force-new-deployment \
    --region "${AWS_REGION}" > /dev/null

success "Deployment triggered"

# ─── Step 8: First-time session auth ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  IMPORTANT: First-time Telegram session setup"
echo "═══════════════════════════════════════════════════════════"
echo ""
warn "The bot needs to log in to Telegram ONE TIME to create a session file."
warn "Run this locally BEFORE the ECS container starts:"
echo ""
echo "  python kelly_telegram_bot.py --personality kelly_persona.yaml --small-model"
echo ""
warn "Enter your phone number and verification code when prompted."
warn "This creates kelly_session.session — upload it to S3:"
echo ""
echo "  aws s3 cp kelly_session.session s3://${PROJECT}-media-${ACCOUNT_ID}/session/kelly_session.session"
echo ""
warn "Then update the ECS task to load it from S3 at startup."

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  BOOTSTRAP COMPLETE"
echo "═══════════════════════════════════════════════════════════"
echo ""
success "Monitoring dashboard auth token: ${MONITOR_AUTH_TOKEN}"
warn "Save this token — it won't be shown again. It's in Secrets Manager."
echo ""
success "ECR URL   : ${ECR_URL}"
success "Cluster   : ${CLUSTER_NAME}"
success "Service   : ${SERVICE_NAME}"
echo ""
