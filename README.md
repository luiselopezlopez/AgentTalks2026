# AgentTalks2026

AgentTalks2026 is a single-container web app that combines:

- A React + Vite frontend that presents a course catalog UI.
- A FastAPI backend in `luiseagent/` that brokers Azure Voice Live / Foundry sessions.
- A browser WebSocket connection at `/ws` for real-time audio, transcript, and avatar events.
- Optional avatar/video negotiation over WebRTC when the agent session supports it.

The frontend build is served by the Python backend, so the deployed site and the voice WebSocket share the same origin.

## Repository layout

- `src/`: React frontend.
- `src/data/courses.ts`: Static catalog data shown in the UI.
- `luiseagent/server.py`: FastAPI app, `/health`, `/ws`, and SPA file serving.
- `luiseagent/app.py`: Older local audio client prototype.
- `scripts/deploy-appservice.ps1`: Azure App Service deployment script.
- `Dockerfile`: Multi-stage image build for frontend + backend.

## Prerequisites

- Node.js 20+
- Python 3.11+
- Azure CLI logged into an identity that can access the Azure AI / Foundry resources used by the agent
- On local Python runs, system audio support may be required for `PyAudio`

## Configuration

The backend loads environment variables from `luiseagent/.env` and from the process environment.

Required settings for a real agent session:

- `AZURE_VOICELIVE_ENDPOINT`
- `AZURE_VOICELIVE_AGENT_ID`
- `AZURE_VOICELIVE_PROJECT_NAME`

Common optional settings:

- `AZURE_VOICELIVE_AGENT_VERSION`
- `AZURE_VOICELIVE_VOICE`
- `AZURE_VOICELIVE_TRANSCRIPTION_MODEL`
- `AZURE_AVATAR_CHARACTER`
- `AZURE_AVATAR_MODEL`
- `AZURE_VOICELIVE_CONVERSATION_ID`
- `AZURE_VOICELIVE_FOUNDRY_RESOURCE_OVERRIDE`
- `AZURE_VOICELIVE_AUTH_IDENTITY_CLIENT_ID`
- `PORT` or `SERVER_PORT`
- `SERVER_HOST`

Minimal example:

```env
AZURE_VOICELIVE_ENDPOINT=https://<your-resource>.services.ai.azure.com/
AZURE_VOICELIVE_AGENT_ID=AgentTalks2026
AZURE_VOICELIVE_PROJECT_NAME=AgentTalks2026
AZURE_VOICELIVE_AGENT_VERSION=16
AZURE_VOICELIVE_VOICE=es-ES-AbrilNeural
```

## Local development

The current app expects the frontend and `/ws` endpoint to be on the same origin.
For local development, Vite now proxies `/ws` and `/health` to the FastAPI backend on `127.0.0.1:8765` by default.

Install frontend dependencies:

```bash
npm install
```

Install backend dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r luiseagent/requirements.txt
```

Start the backend from the repository root:

```bash
python luiseagent/server.py
```

In a second terminal, start the frontend dev server:

```bash
npm run dev
```

Open the Vite URL shown in the terminal, typically `http://127.0.0.1:5173`.

If you prefer serving the built frontend from FastAPI instead, build the frontend:

```bash
npm run build
```

Then run:

```bash
python luiseagent/server.py
```

By default the backend starts on `http://127.0.0.1:8765` and exposes:

- `GET /health`
- `GET /` for the built frontend
- `WS /ws` for voice and avatar events

Logs are written to `luiseagent/logs/`.

## Validation

Frontend build:

```bash
npm run build
```

Backend syntax check:

```bash
python -m py_compile luiseagent/server.py
```

## Docker

Build the combined image from the repository root:

```bash
docker build -t agenttalks2026 .
```

Run it locally with the PowerShell helper:

```powershell
./scripts/run-docker-local.ps1
```

Useful options:

```powershell
./scripts/run-docker-local.ps1 -Detach
./scripts/run-docker-local.ps1 -SkipBuild
./scripts/run-docker-local.ps1 -HostPort 8080
```

The script:

- Builds the image unless `-SkipBuild` is used.
- Loads runtime settings from `luiseagent/.env` with `--env-file`.
- Maps `localhost:<HostPort>` to container port `8000`.
- Mounts `luiseagent/logs/` so local logs are preserved outside the container.

Equivalent manual command:

```bash
docker run --rm -p 8000:8000 \
  --env-file luiseagent/.env \
  -e PORT=8000 \
  agenttalks2026
```

Open `http://localhost:8000`.

Important: the container cannot automatically reuse your host Azure CLI session. If the app must authenticate with Microsoft Entra ID, provide a supported credential inside `luiseagent/.env`, typically `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_CLIENT_SECRET` for a service principal.

## Azure App Service deployment

The repository is set up for a single Linux App Service running a custom container.

Current deployment automation lives in `scripts/deploy-appservice.ps1` and provisions or updates:

- Resource group
- Azure Container Registry
- Linux App Service plan
- Web App configured to pull the container image from ACR
- App settings and managed identity role assignments required by the script

Typical App Service settings:

- `WEBSITES_PORT=8000`
- `PORT=8000`
- `AZURE_VOICELIVE_ENDPOINT`
- `AZURE_VOICELIVE_AGENT_ID`
- `AZURE_VOICELIVE_PROJECT_NAME`
- `AZURE_VOICELIVE_AGENT_VERSION`
- `AZURE_VOICELIVE_VOICE`
- `AZURE_AVATAR_CHARACTER`
- `AZURE_AVATAR_MODEL`

Operational notes:

- Enable WebSockets on the Web App.
- `DefaultAzureCredential` is used by the backend, which works with Azure CLI locally and Managed Identity in Azure.
- Using a unique image tag per deployment is safer than reusing `latest`.

## Notes

- The frontend derives the WebSocket URL from `window.location`, so deployment assumes same-origin hosting.
- The backend allows localhost origins for local use and same-origin requests for deployed environments.
- If avatar negotiation is unavailable for the session, the app can continue in audio mode.
