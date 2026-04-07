# Docker Deployment for OpenSpace

This guide provides instructions on how to run OpenSpace in a Docker container. Containerization ensures a consistent environment, making it easy to run the dashboard and backend API without dealing with local Python and Node.js dependencies.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## Quick Start (Docker Compose)

The easiest way to get OpenSpace running is using `docker-compose`.

1. **Clone the repository:**

   ```bash
   git clone https://github.com/HKUDS/OpenSpace.git
   cd OpenSpace
   ```

2. **Configure Environment Variables:**

   Create a `.env` file in the root directory and add your API keys:

   ```bash
   cp .env.example .env
   # Edit .env with your favorite editor and set your API keys:
   # OPENSPACE_API_KEY=your_key
   # OPENAI_API_KEY=your_key
   # ANTHROPIC_API_KEY=your_key
   ```

3. **Build and Run:**

   ```bash
   docker-compose up -d --build
   ```

4. **Access the Dashboard:**

   Open your browser and navigate to [http://localhost:7788](http://localhost:7788). The frontend and backend are both served seamlessly from the same container.

## Interacting with the CLI

You can use the container to execute OpenSpace CLI commands.

**To run a single task using the CLI inside the running container:**

```bash
docker exec -it openspace openspace --model "anthropic/claude-sonnet-4-5" --query "Analyze the local skills"
```

**To download/upload skills:**

```bash
docker exec -it openspace openspace-download-skill <skill_id>
docker exec -it openspace openspace-upload-skill /app/skills/my-skill
```

## Volumes

The `docker-compose.yml` is configured with two persistent volumes:

- `openspace-data`: Mounted to `/app/.openspace`, storing the SQLite database (`openspace.db`) containing the skill evolution history and metadata.
- `openspace-skills`: Mounted to `/app/skills`, allowing you to persist downloaded and custom skills across container restarts.

If you prefer to mount a local directory for your skills so you can edit them directly from your host machine, you can update the `docker-compose.yml` file:

```yaml
    volumes:
      - openspace-data:/app/.openspace
      - ./my-local-skills:/app/skills
```
