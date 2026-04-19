#!/usr/bin/env bash
# =============================================================================
# deploy.sh — KellyFindomBot Deploy (build + push + force-new-deployment)
# =============================================================================
# Usage:
#   ./deploy/deploy.sh [--region us-east-1] [--env prod]
# =============================================================================
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${1:-prod}"
PROJECT="kelly-${ENVIRONMENT}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT}-bot"
IMAGE_TAG=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)

echo "[DEPLOY] Building image ${IMAGE_TAG} for ${PROJECT}..."

aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_URL}"

docker build -t "${ECR_URL}:${IMAGE_TAG}" -t "${ECR_URL}:latest" .
docker push "${ECR_URL}:${IMAGE_TAG}"
docker push "${ECR_URL}:latest"

echo "[DEPLOY] Triggering ECS deployment..."
aws ecs update-service \
    --cluster "${PROJECT}-cluster" \
    --service "${PROJECT}-bot" \
    --force-new-deployment \
    --region "${AWS_REGION}" > /dev/null

echo "[DEPLOY] Waiting for service stability..."
aws ecs wait services-stable \
    --cluster "${PROJECT}-cluster" \
    --services "${PROJECT}-bot" \
    --region "${AWS_REGION}"

echo "[DEPLOY] ✓ Deployed ${IMAGE_TAG} successfully"
