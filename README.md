# Plane-GitHub Sync Bot

A bidirectional synchronization bot that keeps issue statuses in sync between [Plane.so](https://plane.so) and GitHub.

## Overview

This bot fills the gap in Plane's native GitHub integration, which only handles Pull Requests. It provides:

- **GitHub -> Plane**: When a GitHub issue is closed or reopened, the linked Plane work item status is updated
- **Plane -> GitHub**: When a Plane work item status changes, the linked GitHub issue state is updated

## Features

- Bidirectional status synchronization
- GitHub App authentication (portable, not tied to personal accounts)
- Configurable status mappings via YAML
- Loop prevention to avoid infinite sync cycles
- Rate limiting compliance (Plane: 60 req/min)
- Stateless design (no database required)
- Docker-ready deployment

## Architecture

```
GitHub                          Bot                           Plane
  |                              |                              |
  |-- issue closed/reopened ---->|                              |
  |                              |-- update work item state --->|
  |                              |                              |
  |                              |<-- work item status changed -|
  |<-- update issue state -------|                              |
```

## Prerequisites

- Python 3.11+
- A [GitHub App](https://docs.github.com/en/apps) installed on your repository
- A [Plane.so](https://plane.so) account with API access

## Installation

### Using pip

```bash
# Clone the repository
git clone https://github.com/TristanHerou/Bot-plane.git
cd Bot-plane

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or: .venv\Scripts\activate  # Windows

# Install dependencies
pip install -e .

# Copy and configure environment
cp .env.example .env
# Edit .env with your configuration
```

### Using Docker

```bash
# Build the image
docker build -t plane-github-sync .

# Run with environment variables
docker run -p 8000:8000 \
  -e PLANE_API_KEY=plane_api_xxx \
  -e PLANE_WORKSPACE_SLUG=my-workspace \
  -e PLANE_PROJECT_ID=uuid \
  -e GITHUB_APP_ID=123456 \
  -e GITHUB_WEBHOOK_SECRET=secret \
  -e GITHUB_APP_PRIVATE_KEY_BASE64=base64key \
  plane-github-sync
```

### Using Docker Compose

```bash
# Copy environment file
cp .env.example .env
# Edit .env with your configuration

# Run production
docker compose up -d bot

# Run development with hot reload
docker compose --profile dev up bot-dev
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PLANE_API_KEY` | Yes | Plane API key (format: `plane_api_xxxxx`) |
| `PLANE_WORKSPACE_SLUG` | Yes | Your Plane workspace slug |
| `PLANE_PROJECT_ID` | No | Plane project UUID. If unset, the bot uses **all projects** in the workspace. |
| `PLANE_BASE_URL` | No | Plane API base URL (default: `https://api.plane.so`) |
| `GITHUB_APP_ID` | Yes | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Yes* | Path to GitHub App private key PEM file |
| `GITHUB_APP_PRIVATE_KEY_BASE64` | Yes* | Base64-encoded GitHub App private key |
| `GITHUB_WEBHOOK_SECRET` | Yes | Secret for verifying GitHub webhooks |
| `STATUS_MAPPING_FILE` | No | Path to status mapping YAML (default: built-in mapping) |
| `PORT` | No | Port to run on (default: `8000`) |
| `LOG_LEVEL` | No | Logging level (default: `INFO`) |
| `DEBUG` | No | Enable debug mode (default: `false`) |

*Either `GITHUB_APP_PRIVATE_KEY_PATH` or `GITHUB_APP_PRIVATE_KEY_BASE64` must be set.

### Status Mapping

Create a `config/status-mapping.yml` file to customize status mappings:

```yaml
# GitHub action to Plane status mapping
github_to_plane:
  closed: "Done"
  reopened: "Backlog"

# Plane status to GitHub state mapping
plane_to_github:
  Done: "closed"
  Cancelled: "closed"
  Backlog: "open"
  Todo: "open"
  "In Progress": "open"

# Optional: Pre-configure Plane state IDs
plane_state_ids:
  Done: "uuid-of-done-state"
```

## GitHub App Setup

1. Go to **Settings > Developer settings > GitHub Apps > New GitHub App**

2. Configure the app:
   - **Name**: `Plane Sync Bot` (or your preferred name)
   - **Homepage URL**: Your bot's URL
   - **Webhook URL**: `https://your-bot.example.com/webhooks/github`
   - **Webhook secret**: Generate a secure random string

3. Set permissions:
   - **Repository permissions**:
     - Issues: Read & Write
     - Metadata: Read-only

4. Subscribe to events:
   - Issues

5. Generate and download the private key (PEM file)

6. Install the app on your repository

## Plane Webhook Setup

1. Go to **Workspace Settings > Webhooks**

2. Create a new webhook:
   - **URL**: `https://your-bot.example.com/webhooks/plane`
   - **Events**: Work Item (or all events)

3. Save the webhook

## Running the Bot

### Development

```bash
# With hot reload
python -m uvicorn app.main:app --reload --port 8000

# Or using the CLI entry point
plane-github-sync
```

### Production

```bash
# Using uvicorn directly
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Using Docker
docker compose up -d bot
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Bot information |
| `/health` | GET | Health check |
| `/webhooks/github` | POST | GitHub webhook receiver |
| `/webhooks/github/health` | GET | GitHub webhook health check |
| `/webhooks/plane` | POST | Plane webhook receiver |
| `/webhooks/plane/health` | GET | Plane webhook health check |

## Loop Prevention

The bot implements several mechanisms to prevent infinite sync loops:

1. **Sync cooldown**: Duplicate syncs for the same item within 30 seconds are ignored
2. **Label tracking**: A `plane-sync` label is added to GitHub issues updated by the bot
3. **State comparison**: Updates are skipped if the target already has the desired state

## Development

### Project Structure

```
Bot-plane/
├── app/
│   ├── __init__.py
│   ├── config.py           # Pydantic Settings configuration
│   ├── main.py             # FastAPI application
│   ├── models/
│   │   ├── github.py       # GitHub webhook models
│   │   └── plane.py        # Plane webhook/API models
│   ├── services/
│   │   ├── github_service.py   # GitHub API client
│   │   ├── plane_service.py    # Plane API client
│   │   └── sync_service.py     # Sync orchestration
│   └── webhooks/
│       ├── github.py       # GitHub webhook endpoint
│       └── plane.py        # Plane webhook endpoint
├── config/
│   └── status-mapping.yml  # Status mapping configuration
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── README.md
```

### Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# With coverage
pytest --cov=app
```

### Code Style

```bash
# Format and lint
ruff check --fix app/
ruff format app/

# Type checking
mypy app/
```

## Troubleshooting

### Common Issues

1. **"GitHub App not installed on repository"**
   - Ensure the GitHub App is installed on the repository
   - Check that the App ID and private key are correct

2. **"Plane API key invalid"**
   - Verify the API key starts with `plane_api_`
   - Check the key has access to the specified workspace/project

3. **"No Plane work item linked to GitHub issue"**
   - The Plane work item must have a link to the GitHub issue URL
   - Links are typically added automatically by Plane's native GitHub integration

4. **Rate limiting errors**
   - The bot respects Plane's 60 req/min limit
   - For high-volume scenarios, consider implementing a queue

### Logs

Set `LOG_LEVEL=DEBUG` for verbose logging:

```bash
LOG_LEVEL=DEBUG python -m uvicorn app.main:app --reload
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.
