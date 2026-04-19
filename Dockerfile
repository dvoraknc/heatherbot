FROM python:3.11-slim

# Security: run as non-root
RUN groupadd -r kelly && useradd -r -g kelly kelly

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install AWS SDK for Secrets Manager integration
RUN pip install --no-cache-dir boto3>=1.34 botocore>=1.34

# Copy application code
COPY heather_telegram_bot.py .
COPY kelly_telegram_bot.py .
COPY user_memory.py .
COPY postprocess.py .
COPY aws_secrets_loader.py .
COPY kelly_persona.yaml .
COPY heather_kink_personas.yaml .
COPY persona_example.yaml .

# Create necessary directories
RUN mkdir -p /app/logs /app/user_profiles /app/images_db /app/videos \
    && chown -R kelly:kelly /app

# Health check — confirms bot process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.exists('/app/logs/kelly_bot.log') else 1)"

USER kelly

# Entrypoint uses aws_secrets_loader to pull env from Secrets Manager at start
ENTRYPOINT ["python", "aws_secrets_loader.py"]
CMD ["--personality", "kelly_persona.yaml", "--monitoring", "--small-model"]
