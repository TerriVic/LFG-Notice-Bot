# syntax=docker/dockerfile:1

# Stage 1: Build virtual environment and install dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies if required for compiling C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# Stage 2: Final runtime image
FROM python:3.11-slim

WORKDIR /app

# Prevent Python from buffering stdout so logs appear in real time in Docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Create persistent data folder for SQLite database / tokens
RUN mkdir -p /data

# Copy project files into the container
COPY . .

# Run the bot
CMD ["python", "bot.py"]