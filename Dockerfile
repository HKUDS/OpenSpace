# Stage 1: Build the frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend

# Copy package files and install dependencies (cached layer)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --only=production

# Copy source and build
COPY frontend/ ./
RUN npm run build

# Stage 2: Build the backend
FROM python:3.12-slim

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Install system dependencies required for python packages
# Separate apt commands to leverage cache and clean up in same layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libx11-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy backend source
COPY --chown=appuser:appuser . /app/

# Switch to non-root user for pip install (when possible)
# Some packages may need system deps, but we installed them as root above
USER appuser

# Install Python package with minimal dependencies (linux extras require additional system deps)
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir -e .

# Copy built frontend from Stage 1 (needs root to change ownership)
USER root
COPY --from=frontend-builder --chown=appuser:appuser /app/frontend/dist /app/frontend/dist

# Switch back to non-root user
USER appuser

# Expose the dashboard port
EXPOSE 7788

# Set environment variables
ENV HOST=0.0.0.0
ENV PORT=7788
ENV OPENSPACE_WORKSPACE=/app
ENV PYTHONUNBUFFERED=1

# Create necessary directories with proper permissions
RUN mkdir -p /app/.openspace /app/skills && \
    chmod 700 /app/.openspace /app/skills

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7788/health', timeout=5)" || exit 1

# Run the dashboard server by default
CMD ["openspace-dashboard", "--host", "0.0.0.0", "--port", "7788"]
