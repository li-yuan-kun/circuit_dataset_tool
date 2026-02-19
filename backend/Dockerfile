# syntax=docker/dockerfile:1

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# (Optional) OS packages. Keep minimal; wheels cover most deps.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libjpeg62-turbo zlib1g \
#   && rm -rf /var/lib/apt/lists/*

# Install python deps first for better layer caching
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/backend/requirements.txt

# Copy source code
COPY backend /app/backend
COPY shared /app/shared

# Default dataset output inside container
RUN mkdir -p /app/backend/dataset_output
ENV CDT_DATASET_ROOT=/app/backend/dataset_output

EXPOSE 8000

# Exec-form CMD (JSON array) to avoid shell issues
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
