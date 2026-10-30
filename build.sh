#!/bin/bash

# Build script with BuildKit optimizations
set -e

echo "Building Docker image with BuildKit optimizations..."

# Enable BuildKit
export DOCKER_BUILDKIT=1

# Build with cache mount and BuildKit features
docker build \
    --target production \
    --cache-from=type=local,src=/tmp/.buildx-cache \
    --cache-to=type=local,dest=/tmp/.buildx-cache-new \
    --tag merge-conflict-app:latest \
    .

# Move cache to final location
rm -rf /tmp/.buildx-cache
mv /tmp/.buildx-cache-new /tmp/.buildx-cache

echo "Build completed successfully!"
echo "Image tagged as: merge-conflict-app:latest"
