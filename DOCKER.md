# Docker Deployment for OpenSpace

This guide provides instructions on how to run OpenSpace in a Docker container. Containerization ensures a consistent environment, making it easy to run the dashboard and backend API without dealing with local Python and Node.js dependencies.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (20.10+)
- [Docker Compose](https://docs.docker.com/compose/install/) (v2.0+)

## Quick Start (Docker Compose)

The easiest way to get OpenSpace running is using `docker-compose`.

### 1. Clone the Repository

```bash
git clone https://github.com/HKUDS/OpenSpace.git
cd OpenSpace
git checkout feat/docker-deployment
```

### 2. Configure Environment Variables

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env with your configuration
# Required at minimum:
#   OPENSPACE_API_KEY=$(openssl rand -base64 32)
#
# Choose and configure ONE LLM provider:
#   - OpenAI + OPENAI_API_KEY
#   - Anthropic + ANTHROPIC_API_KEY
#   - OpenRouter + OPENSPACE_LLM_API_KEY + OPENSPACE_LLM_API_BASE + OPENSPACE_MODEL
```

**Environment Variables Reference:**

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENSPACE_API_KEY` | **Yes** | Secret API key for this OpenSpace instance. Generate with `openssl rand -base64 32` |
| `OPENAI_API_KEY` | No | OpenAI API key (sk-...) |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (sk-ant-...) |
| `OPENSPACE_MODEL` | No | Default model name (e.g., `claude-3-7-sonnet-latest`, `stepfun/step-3.5-flash:free`) |
| `OPENSPACE_LLM_API_KEY` | No | API key for custom LLM provider |
| `OPENSPACE_LLM_API_BASE` | No | Base URL for custom LLM provider (e.g., `https://openrouter.ai/api/v1`) |
| `OPENSPACE_DEBUG` | No | Set to `1` to enable debug logging |
| `HOST_PORT` | No | Host port to expose (default: `9001`) |
| `VOLUME_TYPE` | No | `named` (default) or `bind` for local directories |

### 3. Build and Run

```bash
# Standard docker-compose (uses docker-compose.yml)
docker compose up -d --build

# For production with stricter resource limits:
# docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# To stop:
docker compose down

# To view logs:
docker compose logs -f openspace

# To check health status:
docker compose ps
```

### 4. Access the Dashboard

Open your browser and navigate to:

- **Dashboard:** http://localhost:${HOST_PORT:-9001}
- Health endpoint: http://localhost:${HOST_PORT:-9001}/health

The frontend and backend are served seamlessly from the same container.

---

## CLI Usage

You can use the container to execute OpenSpace CLI commands.

### Run a Query

```bash
docker exec -it openspace openspace --model "anthropic/claude-sonnet-4.5" --query "Analyze the local skills"
```

### Download/Upload Skills

```bash
docker exec -it openspace openspace-download-skill <skill_id>
docker exec -it openspace openspace-upload-skill /app/skills/my-skill
```

### Enter Container Shell

```bash
docker exec -it openspace bash
```

---

## Volume & Data Management

### Volume Types

1. **Named volumes** (default) - Managed by Docker, good for simple deployments
   - `openspace-data`: Contains SQLite database (`openspace.db`) and skill history
   - `openspace-skills`: Persists downloaded and custom skills

2. **Bind mounts** - Direct host directory access, better for development
   Edit `docker-compose.yml`:
   ```yaml
   volumes:
     - ./data:/app/.openspace
     - ./skills:/app/skills
   ```

### Backup & Restore

**Backup:**
```bash
# Named volumes
docker run --rm -v openspace-data:/data -v $(pwd):/backup alpine tar czf /backup/openspace-data-$(date +%Y%m%d).tar.gz -C /data .

# Bind mounts (just copy the directories)
cp -r data skills backup/
```

**Restore:**
```bash
# Named volumes
docker run --rm -v openspace-data:/data -v $(pwd):/backup alpine sh -c "rm -rf /data/* && tar xzf /backup/openspace-data-YYYYMMDD.tar.gz -C /data"

# Bind mounts
cp -r backup/data backup/skills ./
```

---

## Monitoring

### Health Check

OpenSpace container includes a health check that pings `/health` endpoint every 30 seconds. Check status:

```bash
docker compose ps
# Look for "healthy" in the STATUS column
```

### Prometheus Metrics

If you have a Prometheus instance, you can scrape metrics from OpenSpace. Add to your `docker-compose.yml`:

```yaml
services:
  openspace:
    # Add this label for service discovery
    labels:
      - "prometheus-job=openspace"
```

Then configure Prometheus to scrape `openspace:7788/metrics` (if endpoint is available).

### Logs

Logs are configured with rotation (10MB max, 3 files by default). View logs:

```bash
docker compose logs -f openspace
# Or with timestamps
docker compose logs -f --timestamp openspace
```

For centralized logging, consider using Loki/Promtail stack (not included by default).

---

## Troubleshooting

### Container fails to start

Check logs:
```bash
docker compose logs openspace
```

Common issues:
- **Missing OPENSPACE_API_KEY**: Set it in `.env` file
- **Port already in use**: Change `HOST_PORT` in `.env` or stop the conflicting service
- **Insufficient memory**: Increase Docker memory limit (Settings → Resources)

### Health check failing

The health check endpoint `/health` might not be available in older versions. If using a development build, you may need to disable the health check by removing it from `docker-compose.yml`.

### Permission denied on volumes

If using bind mounts, ensure the host directories are readable/writable by the container user (UID 1000). Fix with:
```bash
sudo chown -R 1000:1000 data skills
```

---

## Production Deployment Checklist

- [ ] Generate a strong `OPENSPACE_API_KEY` and keep it secret
- [ ] Configure SSL/TLS termination (use reverse proxy like nginx or Traefik)
- [ ] Set appropriate resource limits (memory: 2-4G, CPU: 2-4 cores)
- [ ] Enable log rotation and set up log aggregation
- [ ] Configure regular backups of `openspace-data` volume
- [ ] Set up monitoring (Prometheus + Grafana)
- [ ] Use `docker-compose.prod.yml` for additional production settings
- [ ] Restrict access to the API and dashboard via firewall/VPC
- [ ] Keep Docker and base images updated regularly

---

## Advanced Configuration

### Custom Network

To integrate with other services on a custom network:

```yaml
networks:
  app-network:
    driver: bridge

services:
  openspace:
    networks:
      - app-network
```

### Multi-stage Deployment (with separate frontend/backend)

For large-scale deployments, you might split frontend and backend services. See `docker-compose.multi.yml` (if available).

### Environment-Specific Configs

Use multiple compose files:

```bash
# Development (with hot-reload, less resource limits)
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

# Production (strict limits, optimized)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## Security Notes

- The Dockerfile creates and uses a non-root user `appuser` (UID 1000)
- `OPENSPACE_API_KEY` should be treated as a secret; rotate periodically
- Network access is limited to what the container needs; avoid `--network host`
- Keep the host and Docker daemon updated to prevent vulnerabilities

---

## Contributing

Found an issue or want to improve the Docker deployment? PRs welcome!

Please update:
- `docker-compose.yml` (core config)
- `Dockerfile` (build instructions)
- `DOCKER.md` (this documentation)
- Add/maintain `.env.example`

---

## License

MIT
