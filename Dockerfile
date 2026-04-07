# Stage 1: Build the frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Build the backend and serve
FROM python:3.12-slim
WORKDIR /app

# Install system dependencies required for python packages and GUI features
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libx11-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy backend source
COPY . /app/

# Install the Python package with linux optional dependencies
RUN pip install --no-cache-dir -e .[linux]

# Copy built frontend from Stage 1
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Expose the dashboard port
EXPOSE 7788

# Set environment variables
ENV HOST=0.0.0.0
ENV PORT=7788
ENV OPENSPACE_WORKSPACE=/app

# Run the dashboard server by default
CMD ["openspace-dashboard", "--host", "0.0.0.0", "--port", "7788"]
