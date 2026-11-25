# TaskFlow AI

TaskFlow AI is a workflow automation and multi-agent orchestration platform. This repository currently contains the Phase 1 application foundation; AI planning and workflow execution are intentionally not implemented.

## Requirements

- Docker Desktop with Docker Compose
- For local development: Python 3.12+, Node.js 22+, and npm

## Run with Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open the frontend at <http://localhost:8080>. The API is available at <http://localhost:8000>, with interactive documentation at <http://localhost:8000/docs>. PostgreSQL and Redis are exposed on their default local ports for development.

To stop the services, run `docker compose down`. Persistent PostgreSQL data is kept in the `postgres_data` volume; `docker compose down -v` removes that data.

The Compose file uses development-only database credentials. Do not use them in a deployed environment.

## Local development

Backend, from the repository root:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements-dev.txt
python -m uvicorn app.main:app --app-dir backend --reload
```

Frontend, in a second terminal:

```powershell
Set-Location frontend
npm install
npm run dev
```

The Vite development server is available at <http://localhost:5173> and proxies `/api` requests to the local backend on port `8000`.

## Validation

From the repository root:

```powershell
python -m pytest -c backend\pyproject.toml backend\tests
Set-Location frontend
npm run build
```

## API foundation

- `GET /api/v1/health/live` — process liveness.
- `GET /api/v1/health/ready` — PostgreSQL and Redis connectivity; responds with `503` when either dependency is unavailable.
- `/docs` — interactive OpenAPI documentation.

The versioned API currently contains only health endpoints. See [docs/architecture.md](docs/architecture.md) for responsibility boundaries and the planned architecture.
