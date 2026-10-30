# Using pre-built Microsoft Playwright image with all dependencies
FROM --platform=linux/amd64 mcr.microsoft.com/playwright/python:v1.55.0-jammy AS base

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies with pip cache
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Install Xvfb and xauth for headless browser support
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        xauth \
    && rm -rf /var/lib/apt/lists/*

# Copy the source code
COPY src/ ./src/

# Set Python path
ENV PYTHONPATH=/app:/app/src

# Expose the port
EXPOSE 8080

# Command to run the application
CMD ["python", "-m", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8080"]