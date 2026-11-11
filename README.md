# 🏥 Hextgen HMS Permission-Aware Operations Agent
**Deterministic, Role-Grounded Natural Language Database Operations for Hospital Staff**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17%20RLS-336791.svg)](https://www.postgresql.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests Passing](https://img.shields.io/badge/pytest-31%20passed-brightgreen.svg)]()
[![Eval Accuracy](https://img.shields.io/badge/Eval%20Accuracy-100%25-success.svg)]()
[![Security False-Allows](https://img.shields.io/badge/False--Allow%20Rate-0.00%25-red.svg)]()

---

## 1. Architecture

```mermaid
flowchart TD
    Client["Client (Web Chat UI + Role Switcher / CLI)"] -->|"POST /chat {message}<br/>Authorization: Bearer &lt;JWT&gt;"| FastAPI["FastAPI Application"]
    FastAPI -->|"auth dependency (Token ONLY)"| PrincipalContext["Principal(user_id, hospital_id, roles, effective_perms)"]
    
    subgraph AgentLayer["1. Agent Layer (Gemini LLM)"]
        PreProc["Context Pre-processor (IST Asia/Kolkata date)"]
        LLMParser["LLM Intent Parser (Schema-validated Pydantic union)"]
        PreProc --> LLMParser
    end
    
    FastAPI --> AgentLayer
    LLMParser -->|"Intent JSON (Identity dropped)"| PolicyLayer
    PrincipalContext --> PolicyLayer
    
    subgraph PolicyLayer["2. Policy Layer (Pure Python, Deterministic)"]
        P1["1. Inactive User Check"]
        P2["2. Supported Operation Check"]
        P3["3. Cross-Tenant Guard"]
        P4["4. Permission Matrix Check"]
        P5["5. Constraints (max_rows, max_pct, own_only)"]
        P6["6. Access Rules (Escalation, Self-lockout, Ambiguity)"]
        P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end
    
    P6 -->|"REFUSE"| ExitRefuse["Refusal + Reason (Zero Leak)"]
    P6 -->|"CLARIFY"| ExitClarify["Clarifying Question"]
    P6 -->|"ALLOW (Read)"| ExecRead["ops_read.py (Parameterized, hospital_id scoped)"]
    P6 -->|"ALLOW_WITH_PREVIEW (Write)"| DryRun["dryrun.py (Savepoint Rollback Preview + Token)"]
    
    subgraph ExecLayer["3. Execution Layer (PostgreSQL)"]
        ExecRead --> PostgreSQL[("PostgreSQL DB (RLS + SET LOCAL app.hospital_id)")]
        DryRun -->|"Pending Token (5 min expiry)"| PendingActions[("pending_actions Table")]
        ConfirmReq["POST /confirm {token, typed_count}"] --> ConfirmOp["confirm.py (Re-check policy + Blast-radius check)"]
        ConfirmOp --> PostgreSQL
    end
    
    subgraph AuditLayer["4. Audit Layer (Router finally block)"]
        AuditRecord["audit.py record_audit()"]
        AuditLog[("audit_log Table (1 row per attempt)")]
        AuditRecord --> AuditLog
    end
    
    FastAPI -.-> AuditRecord
```

---

## 2. Hard Requirements Compliance Matrix

| # | Requirement | Implementation | Guarantee |
|---|---|---|---|
| **1** | **Enforcement in Code** | [`app/policy/engine.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/policy/engine.py) + [`app/auth.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/auth.py) | User identity never passed to LLM; model proposes, code decides. |
| **2** | **No Raw LLM SQL** | [`app/schemas.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/schemas.py) + [`app/execution/`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/) | Typed Operation Mapping (`Intent` union) executed via parameterized SQLAlchemy statements. |
| **3** | **Tenant Isolation & RLS** | [`db/init/01_schema.sql`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/db/init/01_schema.sql) | Double-layer isolation: `WHERE hospital_id = ?` + Postgres Row-Level Security (`SET LOCAL app.hospital_id`). |
| **4** | **Dry Run Before Every Write** | [`app/execution/dryrun.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/dryrun.py) | Savepoint dry-run shows rows affected, ₹ financial impact, and 5 before/after samples. Requires token confirmation. |
| **5** | **Blast-Radius Guard** | [`app/execution/confirm.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/confirm.py) | Any write touching > 50 rows requires user to type the exact row count to confirm. |
| **6** | **Atomic Execution** | [`app/execution/confirm.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/confirm.py) | Atomic transactions with immediate rollback on exceptions. |
| **7** | **Audit Everything** | [`app/audit.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/audit.py) | Every request (including refusals and errors) writes 1 row to `audit_logs` in a `finally` block. |
| **8** | **Clarify, Don't Guess** | [`app/policy/rules.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/policy/rules.py) | Ambiguous users or permissions produce explicit clarifying questions. |
| **9** | **Dates in IST (Asia/Kolkata)** | [`app/agent/dates.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/agent/dates.py) | "Last month", "today", and "this week" resolve in Asia/Kolkata timezone. |

---

## 3. Stretch Goals Implemented & Verified

### 1. Undo ("Undo my last change")
- Reverts the user's latest executed mutation atomically using the stored `before_snapshot`.
- **Concurrency Guard:** Checks each row's current state against `after_snapshot`. If another transaction altered the rows in the interim, undo is safely blocked.

### 2. Approval Routing
- When a Billing Manager requests a discount > 10% (e.g. 20%), the request is automatically routed into a pending approval (`#REQ-xxx`).
- Non-admin approval attempts are refused with `Permission denied`.
- Administrators can review pending requests and approve directly in chat.

### 3. Telugu Regional Input
- Natural language parsing support for Telugu commands (e.g., *"Last month bills ki 20% discount ivvu"*, *"Expiring medicines chupinchu"*, *"Paracetamol stock 100 ki update cheyyi"*).

### 4. Evaluation Harness (`eval/run_eval.py`)
- Automated benchmark across 56 labelled queries covering all 5 roles:
  - **Intent Parsing Accuracy:** `100.0%`
  - **Policy Decision Accuracy:** `100.0%`
  - **Security False-Allow Rate:** `0.00%` (Target: 0.0%)
  - **Operational False-Deny Rate:** `0.00%`

### 5. Minimal Chat Web UI (`GET /`)
- Single-page responsive interface with a role switcher, Telugu prompt chips, quick action buttons ("Undo", "Pending Approvals"), and blast-radius confirmation inputs.

---

## 4. Local Setup & Execution Guide

### Prerequisites
- Python 3.12+
- PostgreSQL 15+ or Supabase account
- Google Gemini API Key (optional, built-in fallback parser works offline)

### Step 1: Environment Configuration
```powershell
copy .env.example .env
```
Update `.env` with your credentials:
```env
ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/hms_db
DATABASE_URL=postgresql://hms_app:hms_app_password@localhost:5432/hms_db
JWT_SECRET=your_32_char_secret_key_here
GEMINI_API_KEY=your_gemini_api_key_here
```

### Step 2: Install Dependencies
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Step 3: Initialize Database & Seed
```powershell
# Option A: Seed to existing database
python -m seed.seed

# Option B: Run via Docker Compose (PostgreSQL 17 with RLS)
docker-compose up -d
```

### Step 4: Run Application
```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Open your browser at:
- **Web UI & Role Switcher:** `http://127.0.0.1:8000/`
- **Interactive Swagger Docs:** `http://127.0.0.1:8000/docs`
- **Health Check:** `http://127.0.0.1:8000/health`

---

## 5. Running the Test Suites & Verification

```powershell
# 1. Run all 31 Unit & Integration Pytest Tests
pytest

# 2. Run the 56-Query Evaluation Benchmark
python -m eval.run_eval

# 3. Run End-to-End Stretch Goals Verification
python test_stretch_goals.py

# 4. Verify All 12 Core Scenarios
python verify_all_12_scenarios.py
```

---

## 6. Docker & Cloud Deployment

### Docker Build & Run
```bash
docker build -t hms-operations-agent .
docker run -p 8000:8000 --env-file .env hms-operations-agent
```

### Deploy to Cloud Platforms (Render / Railway / Fly.io)
A `Procfile` is included for zero-config container/PaaS deployments:
```text
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```
Environment variables to set in cloud dashboard:
- `DATABASE_URL` (Connection string with RLS enabled)
- `ADMIN_DATABASE_URL` (Admin connection string for migrations/seed)
- `JWT_SECRET`
- `GEMINI_API_KEY`
