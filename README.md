
<<<<<<< HEAD
=======
TaskFlow AI is a workflow automation and multi-agent orchestration platform. The repository contains the Phase 1 application foundation, Phase 2 backend/database/authentication foundation, Phase 3 workflow management, Phase 4 AI-assisted workflow planning, Phase 5 safe tool registry/execution foundation, and Phase 6 durable workflow execution.

## Requirements

- Docker Desktop with Docker Compose
- For local development: Python 3.12+, Node.js 22+, and npm

## Run with Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open the frontend at <http://localhost:8080>. The API is available at <http://localhost:8000>, with interactive documentation at <http://localhost:8000/docs>. PostgreSQL and Redis are exposed on their default local ports for development.

The backend container applies pending Alembic migrations before starting the API.

To stop the services, run `docker compose down`. Persistent PostgreSQL data is kept in the `postgres_data` volume; `docker compose down -v` removes that data.

The Compose file uses development-only database credentials. Do not use them in a deployed environment.

## Local development

Backend, from the repository root:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements-dev.txt
python -m alembic -c backend\alembic.ini upgrade head
python -m uvicorn app.main:app --app-dir backend --reload
```

For local development, an unset `JWT_SECRET_KEY` is generated for the process. Set a stable secret for persistent sessions; production requires an explicitly configured secret of at least 32 bytes and `AUTH_COOKIE_SECURE=true`.

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
- `POST /api/v1/auth/register` and `POST /api/v1/auth/login` — create an account or start a session. Responses include a short-lived bearer access token; the rotating refresh token is stored in an HttpOnly, SameSite=Strict cookie.
- `POST /api/v1/auth/refresh` and `POST /api/v1/auth/logout` — rotate or revoke the refresh session. Revocation also invalidates its access tokens.
- `GET /api/v1/auth/me` — current authenticated user.
- `GET/PATCH /api/v1/users/me` — read or update the authenticated user's profile.
- `POST/GET /api/v1/workflows` — create a workflow or list the current user's workflows. Lists support `limit`, `offset`, `search`, `archived`, `published`, `sort_by`, and `sort_order`.
- `GET/PUT/DELETE /api/v1/workflows/{id}` — retrieve, versioned-update, or safely delete an owned workflow. Delete is refused for published workflows or workflows with execution history; archive them instead.
- `POST /api/v1/workflows/{id}/archive`, `/duplicate`, and `/publish` — manage workflow lifecycle. Updates create a new definition snapshot; publishing locks that snapshot while future edits create a new draft version.
- `GET /api/v1/workflows/{id}/versions` and `/versions/{version}` — browse paginated history and retrieve a specific version.
- `POST /api/v1/workflows/{id}/executions` — start an authenticated execution of the currently published version. Supply `{"trigger_data": {...}}`; an optional `Idempotency-Key` header makes a repeated request return the original execution.
- `GET /api/v1/workflows/{id}/executions` and `/executions/{execution_id}` — list paginated execution history or retrieve an owned execution with its persisted step checkpoints.
- `POST /api/v1/workflows/{id}/executions/{execution_id}/approval` — approve a paused checkpoint with `{"approved": true, "reason": "..."}` or reject it with `"approved": false`.
- `POST /api/v1/workflows/{id}/executions/{execution_id}/cancel` — cancel an active, queued, or approval-paused execution.
- `POST /api/v1/workflows/plan` — interpret a natural-language request, plan a declarative workflow with LangGraph and a configured OpenAI or Anthropic provider, validate the tool catalog and persist an unpublished draft for review.
- `GET /api/v1/tools` — list tool metadata, permissions, risk, timeout, and availability.
- `POST /api/v1/tools/{tool_name}/execute` — execute one authenticated, permission-checked registry tool; this does not execute a workflow.
- `GET /api/v1/tools/executions` — list the current user's tool execution records.
- `GET /api/v1/tools/notification-drafts/{id}` — retrieve an owned internal notification draft.
- `/docs` — interactive OpenAPI documentation.

Set `PLANNING_PROVIDER` to `openai` or `anthropic`, configure the matching API key, and set `PLANNING_MODEL` to a model available to that provider. Planning fails explicitly when configuration or credentials are absent; there is no production mock-provider fallback. The planner selects only available tools from the runtime registry. Generated workflows are saved as unpublished drafts and are not executed.

The initial registry provides bounded arithmetic, date/time, text transformation, JSON processing, UTF-8 text extraction from user-provided TXT/Markdown/CSV/JSON bytes, owner-scoped workflow lookup, and private notification draft creation. Notification drafts are stored for review and never sent. Tool attempts and outcomes are recorded with input/output hashes; raw arguments and results are not copied into audit records. Per-user safe-tool permissions are seeded for built-in tools and enforced on every invocation. Email, messaging, CRM, webhook, external API, and document-storage entries are explicitly marked unavailable: they have no connected provider and cannot be executed.

Workflow definitions validate trigger types, step types and inputs, dependencies, conditional branches, timeouts, retries, approvals, and metadata. The dependency/branch/failure graph must be acyclic. Database schema changes are managed with Alembic. PostgreSQL must be reachable before running migrations. Auth and planning endpoints use Redis-backed per-IP rate limits and fail closed if Redis is unavailable. See [docs/architecture.md](docs/architecture.md) for service boundaries and security notes.

Workflow execution uses the published immutable version, persists execution and per-step state, validates registry tool arguments and permissions at dispatch, and supports dependencies, a restricted JSON-literal condition language, retry policies, failure branches, delays, notification drafts, approval pause/resume, cancellation, and idempotent starts. It runs inline in the API process, with a five-minute active-duration cap; there is not yet a background worker, scheduler, or realtime event service. Startup recovery fails stale queued/running executions and reconciles stale cancelled executions with an in-flight step; it never replays an uncertain step. Registered tool calls are capped at 60 seconds; recovery waits 90 seconds without a heartbeat before treating an execution as stale. Only manual API execution is currently available; schedule, webhook, and event triggers are definitions, not active integrations.
>>>>>>> 10f6ecd (feat: implement TaskFlow workflow management and execution)
