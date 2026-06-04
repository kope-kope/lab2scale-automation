# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

# Avoid Python writing .pyc and buffering output (better for cron logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first so layer is cached when only application code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project.
COPY . .

# Ensure runtime data directory exists for SQLite + dry-run report HTML.
# In production this should be a mounted volume so state survives restarts.
RUN mkdir -p /app/data

# Default — Railway services override this via startCommand:
#   sweep service:  python main.py sweep
#   report service: python main.py report
#   weekly cron:    python main.py full   (sweep + report in one shot)
CMD ["python", "main.py", "full"]
