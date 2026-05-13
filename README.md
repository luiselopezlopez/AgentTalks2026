# AgentTalks2026

This repository now supports a single-container deployment model for Azure App Service:

- The React/Vite frontend is compiled during the Docker build.
- The `luiseagent` FastAPI server serves the built frontend from the same host.
- The WebSocket endpoint stays available at `/ws` on the same origin as the site.

## Local validation

Build the frontend:

```bash
npm run build
```

Check Python syntax for the backend:

```bash
python -m py_compile luiseagent/server.py
```

## Docker image

Build the combined image from the repository root:

```bash
docker build -t agenttalks2026 .
```

Run it locally:

```bash
docker run --rm -p 8000:8000 \
	-e PORT=8000 \
	-e AZURE_VOICELIVE_ENDPOINT="<your-endpoint>" \
	-e AZURE_VOICELIVE_AGENT_ID="<agent-id>" \
	-e AZURE_VOICELIVE_PROJECT_NAME="<project-name>" \
	agenttalks2026
```

Open `http://localhost:8000`. The frontend and the `/ws` endpoint will both come from the same origin.

## Azure App Service deployment

Recommended target:

- One Linux App Service using a custom container
- Managed Identity enabled on the web app
- Azure Key Vault for secret-backed app settings

Minimum app settings to configure on the App Service:

- `PORT=8000`
- `AZURE_VOICELIVE_ENDPOINT`
- `AZURE_VOICELIVE_AGENT_ID`
- `AZURE_VOICELIVE_PROJECT_NAME`
- `AZURE_VOICELIVE_AGENT_VERSION` if required by your Foundry agent
- `AZURE_VOICELIVE_FOUNDRY_RESOURCE_OVERRIDE` if required in your environment
- `AZURE_VOICELIVE_AUTH_IDENTITY_CLIENT_ID` if you use a user-assigned managed identity

App Service configuration notes:

- Enable WebSockets
- Set `WEBSITES_PORT` to `8000`
- Grant the managed identity access required for Azure Voice Live / Foundry

## Current implementation notes

- The frontend now derives its WebSocket URL from the current browser origin, so the deployed app does not depend on localhost.
- The backend accepts same-origin WebSocket requests from the deployed site while preserving the localhost development allowlist.
- The backend uses `DefaultAzureCredential`, which works with Managed Identity in Azure and Azure CLI locally.
