# ShopWave Agentic Support Backend (LangGraph + Gemini)

This project processes support tickets from JSON data using a LangGraph workflow with deterministic tools, a Gemini decision node, confidence-aware escalation, and full audit logging.

## What This Backend Delivers

- Typed state graph per ticket with clear, inspectable node transitions.
- Minimum 3+ tool calls per ticket enforced by graph design.
- Required read/write mock tools implemented (`get_*`, `search_knowledge_base`, `issue_refund`, `send_reply`, `escalate`).
- Autonomous action execution on resolved paths (not classification-only).
- Confidence score in every decision and every audit entry.
- Conditional routing to human escalation when confidence is low or policy risk is high.
- Deterministic tool failure simulation (timeouts, malformed, partial responses).
- Retry budgets with exponential backoff and schema validation before action.
- Dead-letter queue artifact for irrecoverable action failures.
- Concurrent ticket handling with a semaphore cap.
- Output artifacts for judging and review.

## Graph Flow

`PARSE_TICKET -> GET_CUSTOMER -> GET_ORDER -> GET_PRODUCT -> SEARCH_KNOWLEDGE_BASE -> CHECK_REFUND_ELIGIBILITY -> DECIDE -> (RESOLVE_ACTIONS | ESCALATE_ACTIONS) -> FINALIZE`

Escalation route is triggered when:

- `decision.needs_escalation = true`, or
- `decision.confidence < 0.65`

## Output Files

After a run, these files are written to `outputs/`:

- `resolutions.json`: Final response per ticket.
- `audit_log.json`: Full per-ticket tool call history and decision traces.
- `escalations.json`: Escalated ticket summaries.
- `dead_letter_queue.json`: Tickets requiring operator intervention after retry exhaustion.
- `summary.json`: Run-level metrics (counts, average confidence, minimum tool calls).

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies.
3. Configure Gemini key via environment.

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set:

```env
GEMINI_API_KEY=YOUR_GEMINI_API_KEY_HERE
GEMINI_MODEL=gemini-1.5-flash
```

## Run

```powershell
python main.py
```

Optional flags:

```powershell
python main.py --max-concurrency 10 --confidence-threshold 0.65 --model gemini-1.5-flash
```

Optional resiliency knob:

```powershell
$env:TOOL_RETRY_BUDGET=2
python main.py
```

## Run Production API Backend (Port 8011)

This repo now includes a production-oriented FastAPI service that wraps the ticket pipeline and exposes job APIs used by the frontend.

Start the backend:

```powershell
python api_server.py
```

Or with uvicorn explicitly:

```powershell
uvicorn api_server:app --host 0.0.0.0 --port 8011
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8011/api/v1/health
```

### API Endpoints

- `GET /api/v1/health`
- `POST /api/v1/jobs`
- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/{job_id}/artifacts/{artifact_name}`

Supported artifact names:

- `summary`
- `resolutions`
- `escalations`
- `dead_letter_queue`
- `audit_log`

### Production Notes

- Default API port is `8011` via `API_PORT`.
- Set `BACKEND_API_KEY` to enforce `x-api-key` auth on job endpoints.
- Tighten `ALLOWED_HOSTS` and `CORS_ORIGINS` in production.
- Job state is in-memory for this hackathon implementation; use one API process instance unless you add shared persistence.

## Project Structure

- `api_server.py`: FastAPI production backend with job APIs and health endpoint.
- `main.py`: Runner, concurrency, artifact writing.
- `support_agent/data_store.py`: JSON data loading and indexing.
- `support_agent/tools.py`: Deterministic tool layer.
- `support_agent/llm.py`: Gemini-based decision node with fallback heuristics.
- `support_agent/graph.py`: LangGraph state machine definition.
- `support_agent/audit.py`: Structured audit logging utilities.
- `support_agent/models.py`: Shared typed models/state.

## Additional Deliverables

- Architecture Diagram: `ARCHITECTURE.md`
- Failure Mode Analysis: `FAILURE_MODE_ANALYSIS.md`

## Git Quick Start

If repository is not initialized:

```powershell
git init
```

Then commit:

```powershell
git add .
git commit -m "Build LangGraph + Gemini ticket support backend"
```

Connect to GitHub and push:

```powershell
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

If `origin` already exists, update it:

```powershell
git remote set-url origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```