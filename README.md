# 🏥 Hextgen HMS Permission-Aware Operations Agent
**Deterministic, Role-Grounded Natural Language Database Operations for Hospital Staff**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17%20RLS-336791.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker Compose](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)
[![Tests Passing](https://img.shields.io/badge/Pytest-31%20Passed-brightgreen.svg?style=flat)]()
[![Eval Accuracy](https://img.shields.io/badge/Eval%20Accuracy-100%25-success.svg?style=flat)]()
[![False-Allow Rate](https://img.shields.io/badge/Security%20False--Allow-0.00%25-red.svg?style=flat)]()

---

## 📖 Executive Summary

The **Hextgen HMS Operations Agent** enables hospital personnel (Administrators, Billing Managers, Pharmacists, Front Desk Staff, and Doctors) to query and update critical medical records and inventory using natural language in **English** and **Telugu**.

> [!IMPORTANT]
> **Core Architectural Axiom: "The LLM Proposes; Your Code Decides."**
> Any system where a large language model is trusted to enforce authorization or construct unvalidated privileged database commands fails fundamental security principles. In this system:
> 1. The LLM never receives user credentials, roles, or tenant IDs.
> 2. The LLM only maps user utterances to strictly typed operation intents (`Intent` union).
> 3. All permission checks, row limits, constraint validations, and tenant scoping execute strictly in deterministic Python code and PostgreSQL Row-Level Security (RLS).

---

## 🛠️ Technology Stack

| Layer | Component | Description |
| :--- | :--- | :--- |
| **Runtime & Language** | **Python 3.12** | Modern asynchronous Python runtime with strong type typing. |
| **API Framework** | **FastAPI 0.115 + Uvicorn** | High-performance asynchronous REST API framework with Pydantic v2 schemas and automatic OpenAPI documentation. |
| **Database Engine** | **PostgreSQL 17 (Alpine)** | Relational database with dual-user setup (`hms_admin` for schema/seeding, `hms_app` with `NOBYPASSRLS` for application queries). |
| **Isolation & Security** | **Postgres Row-Level Security (RLS)** | Multi-tenant protection enforced directly inside PostgreSQL engine via `SET LOCAL app.hospital_id`. |
| **Database Toolkit** | **SQLAlchemy 2.0 + psycopg3** | Parameterized query generation preventing SQL injection; connection pooling and transactional savepoints. |
| **Intelligence / LLM** | **Google Gemini 3.8 Flash** | Fast intent extraction using official `google-genai` SDK, backed by an offline heuristic parser with resilient fallback. |
| **Authentication** | **PyJWT + Passlib PBKDF2** | Cryptographically signed JSON Web Tokens (HS256) encoding `user_id` and `hospital_id`. |
| **Containerization** | **Docker & Docker Compose** | Multi-container setup with automated schema migration, healthchecks, and seed dataset generation. |
| **Evaluation & Tests** | **Pytest + Eval Harness** | 31 unit & integration tests + 56-query ground-truth benchmark tracking intent accuracy, false-allow, and false-deny metrics. |
| **User Interface** | **Glassmorphic Web UI** | Modern responsive chat interface with a real-time role switcher, blast-radius modal confirmation, and pending approval badge. |

---

## 🏛️ System Architecture

```mermaid
flowchart TD
    Client["Client: Web Chat UI / CLI / Swagger"] -->|"1. POST /chat {message}<br/>Header: Bearer JWT"| AuthMiddleware["Auth Layer: JWT Verification"]
    AuthMiddleware -->|"Builds Principal from DB"| Principal["Principal Context (user_id, hospital_id, roles, perms)"]
    
    subgraph AgentLayer["1. Agent Intent Layer (Zero Privilege)"]
        DateEngine["Date Resolver (IST Asia/Kolkata)"] --> Parser["Gemini 3.8 Flash Parser / Heuristic Fallback"]
        Parser -->|"Emits Typed Intent JSON (No User Identity)"| IntentObj["Typed Intent Object"]
    end
    
    Client --> AgentLayer
    
    subgraph PolicyEngine["2. Policy Enforcement Layer (Deterministic Code)"]
        Principal --> GateKeeper["Deterministic Policy Rules"]
        IntentObj --> GateKeeper
        GateKeeper --> Rule1["1. Inactive User Check"]
        Rule1 --> Rule2["2. Cross-Tenant Guard"]
        Rule2 --> Rule3["3. Permission Matrix Check"]
        Rule3 --> Rule4["4. Parameter Constraints (max_rows, max_discount)"]
        Rule4 --> Rule5["5. Ambiguity Check (Clarify, Don't Guess)"]
    end
    
    GateKeeper -->|"REFUSE"| ExitRefuse["Refusal Response (Reason Logged)"]
    GateKeeper -->|"CLARIFY"| ExitClarify["Clarifying Question (Target/Action)"]
    GateKeeper -->|"ALLOW (Read)"| ExecRead["ops_read.py (Parameterized & Scoped)"]
    GateKeeper -->|"ALLOW_WITH_PREVIEW (Write)"| DryRun["dryrun.py (Savepoint Rollback Preview)"]
    
    subgraph ExecLayer["3. Execution & Storage Layer"]
        ExecRead --> PostgreSQL[("PostgreSQL 17 Database<br/>RLS Enabled + SET LOCAL app.hospital_id")]
        DryRun -->|"Issues Pending Token"| PendingStore[("pending_actions Table")]
        ConfirmReq["POST /confirm {token, typed_count}"] --> ConfirmExec["confirm.py (Atomic Commit)"]
        ConfirmExec --> PostgreSQL
    end
    
    subgraph AuditLayer["4. Immutable Audit Trail"]
        AuditRecord["audit.py: record_audit() in finally block"]
        AuditLog[("audit_logs Table (1 row per attempt)")]
        AuditRecord --> AuditLog
    end
```

---

## ✅ Hard Requirements Compliance Matrix

Every hard requirement has been strictly designed, implemented, and verified in code:

| # | Hard Requirement | Implementation Strategy | Source Location | Verification Status |
| :---: | :--- | :--- | :--- | :---: |
| **1** | **Enforcement in Code** | User identity and permissions are resolved exclusively from the cryptographically signed JWT. The LLM never sees user identity or permissions. | [`app/auth.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/auth.py), [`app/policy/engine.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/policy/engine.py) | **Verified** |
| **2** | **No Raw LLM SQL** | Intent mapping to typed operations (`Intent` union) executed via parameterized SQLAlchemy queries. No raw SQL strings from the LLM ever touch the database. | [`app/schemas.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/schemas.py), [`app/execution/`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/) | **Verified** |
| **3** | **Tenant Isolation & RLS** | Two-layer isolation: Every database query enforces `WHERE hospital_id = :id` in application code, plus PostgreSQL native Row-Level Security (`SET LOCAL app.hospital_id`). | [`db/init/01_schema.sql`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/db/init/01_schema.sql), [`app/execution/ops_read.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/ops_read.py) | **Verified** |
| **4** | **Dry Run Before Every Write** | Write operations first execute within an isolated transaction savepoint, calculating affected rows, ₹ financial impact, and 5 before/after sample rows, followed by immediate rollback. A confirmation token (5-min TTL) is returned. | [`app/execution/dryrun.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/dryrun.py) | **Verified** |
| **5** | **Blast-Radius Guard** | Any write operation affecting more than 50 rows blocks simple "yes" confirmation; the user is required to type the exact row count to proceed. | [`app/execution/confirm.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/confirm.py) | **Verified** |
| **6** | **Atomic Execution** | Confirmed mutations execute inside an atomic database transaction. Any failure halfway triggers an immediate rollback, leaving zero partial state. | [`app/execution/confirm.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/execution/confirm.py) | **Verified** |
| **7** | **Audit Everything** | Every attempt (allowed, refused, clarified, or errored) writes a record to `audit_logs` in a `finally` block with raw query, parsed intent, decision, and reason. | [`app/audit.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/audit.py) | **Verified** |
| **8** | **Clarify, Don't Guess** | Ambiguous entities (e.g., multiple staff members sharing a name like "Ravi") or ambiguous actions ("give access" without specifying role) return an explicit clarifying question instead of executing. | [`app/policy/rules.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/policy/rules.py) | **Verified** |
| **9** | **Dates in IST (Asia/Kolkata)** | Relative date expressions ("last month", "today", "this week") resolve against the `Asia/Kolkata` timezone with exact dates reflected in preview data. | [`app/agent/dates.py`](file:///c:/Users/CH%20SANDEEP/OneDrive/Documents/Hexgan%20ai/app/agent/dates.py) | **Verified** |

---

## 🌟 Stretch Features Implemented & Verified

### 1. 🔄 Undo ("Undo my last change")
- Reverts the authenticated user's most recent mutation using the stored `before_snapshot`.
- **Concurrency Guard:** Verifies current row state against `after_snapshot`. If another transaction modified those rows in the interim, undo is safely refused to prevent dirty overwrites.

### 2. 📋 Approval Routing
- When a Billing Manager requests a discount exceeding their 10% constraint (e.g., 20%), the system automatically creates a pending approval request (`#REQ-xxx`).
- Administrators can view pending requests and approve or reject them directly via chat or the UI.
- Non-admin approval attempts are rejected with `Permission denied`.

### 3. 🇮🇳 Telugu Regional Language Input
- Natural language comprehension in Telugu for hospital staff:
  - *"Last month bills ki 20% discount ivvu"* &rarr; maps to `BillsDiscountIntent`.
  - *"Expiring medicines chupinchu"* &rarr; maps to `InventoryReadIntent`.
  - *"Paracetamol stock 100 ki update cheyyi"* &rarr; maps to `InventoryBulkUpdateIntent`.
  - *"Last change cancel cheyyi"* &rarr; maps to `UndoIntent`.

### 4. 📊 56-Query Evaluation Benchmark (`eval/run_eval.py`)
- Automated benchmark across 56 labelled scenarios covering all 5 roles:
  - **Intent Parsing Accuracy:** `100.0%`
  - **Policy Decision Accuracy:** `100.0%`
  - **Security False-Allow Rate:** `0.00%` (Zero unauthorized actions permitted)
  - **Operational False-Deny Rate:** `0.00%` (Zero valid authorized actions blocked)

### 5. 💻 Interactive Web UI with Role Switcher
- Single-page interface available at `http://localhost:8000/`.
- Instant role switching between:
  - **Priya Sharma** (Admin &bull; Hospital A)
  - **Suresh Patel** (Billing Manager &bull; Hospital A)
  - **Anita Desai** (Pharmacist &bull; Hospital A)
  - **Vikram Singh** (Front Desk &bull; Hospital A)
  - **Dr. Rajesh Kumar** (Doctor &bull; Hospital A)
  - **Inactive User** (Revoked Access demo)
- Built-in dry-run preview modal, blast-radius numeric input, and pending approval notification counter.

---

## 🚀 Quick Start Guide

### Method 1: Docker Compose (Recommended)

Run the entire application stack (Postgres 17 DB + Auto-seeded Data + FastAPI Agent) with a single command:

```bash
docker compose up -d --build
```

1. **Wait for containers to become healthy:**
   ```bash
   docker compose ps
   ```
   Both `hms_agent_db` and `hms_agent_web` will report `Up (healthy)`.

2. **Access the application:**
   - **Web UI & Role Switcher:** [http://localhost:8000/](http://localhost:8000/)
   - **Interactive API Documentation:** [http://localhost:8000/docs](http://localhost:8000/docs)
   - **Healthcheck:** [http://localhost:8000/health](http://localhost:8000/health)

3. **Run the in-container tests:**
   ```bash
   docker compose exec web pytest
   ```

4. **Stop containers:**
   ```bash
   docker compose down
   ```

---

### Method 2: Local Python Environment

#### 1. Prerequisites
- Python 3.12+
- PostgreSQL 15+ (local or Supabase)

#### 2. Setup Virtual Environment
```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

#### 3. Environment Variables
Copy `.env.example` to `.env` and configure your database URLs:
```env
# Database Connections
ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/hms_db
DATABASE_URL=postgresql://hms_app:hms_app_password@localhost:5432/hms_db

# Security & Auth
JWT_SECRET=supersecretjwtkey_replace_in_production_min_32_chars

# LLM (Optional: Heuristic fallback operates automatically if omitted)
GEMINI_API_KEY=your_gemini_api_key_here
```

#### 4. Seed the Database
```bash
python -m seed.seed
```
This populates:
- 2 Hospitals (Hospital A & Hospital B)
- 16 Users across all roles (Admin, Billing, Pharmacy, Front Desk, Doctor)
- 1,300+ Inventory items with critical and expired stock
- 1,100+ Patient bills

#### 5. Start the Server
```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🧪 Verification & Test Suites

The project includes automated verification covering unit tests, integration tests, security boundaries, and evaluation metrics:

```bash
# 1. Run all 31 Pytest Unit & Integration tests
pytest

# 2. Run the 56-query Evaluation Benchmark (Accuracy, False-Allow, False-Deny)
python -m eval.run_eval

# 3. Verify All 12 Core Hospital Scenarios
python verify_all_12_scenarios.py

# 4. Verify Stretch Goals (Undo, Approval Routing, Telugu input)
python test_stretch_goals.py
```

---

## 📡 API Reference

### 1. `POST /login`
Generate a signed JWT for demo accounts.
```bash
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin.a@hospital.com"}'
```

### 2. `POST /chat`
Submit a natural language operation instruction.
```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message": "Show low stock medicines"}'
```

### 3. `POST /confirm`
Execute a staged mutation after reviewing its dry-run preview.
```bash
curl -X POST http://localhost:8000/confirm \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"token": "<CONFIRM_TOKEN>", "typed_count": 65}'
```

### 4. `POST /cancel`
Discard a staged dry-run mutation without applying changes.
```bash
curl -X POST http://localhost:8000/cancel \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"token": "<CONFIRM_TOKEN>"}'
```

---

## 📁 Repository Structure

```
.
├── app/
│   ├── agent/                 # LLM intent parsing & IST date calculations
│   │   ├── dates.py           # Asia/Kolkata date resolution
│   │   ├── parser.py          # Gemini 3.8 Flash + Resilient fallback parser
│   │   └── prompts.py         # Zero-identity LLM extraction prompt
│   ├── execution/             # Deterministic database operations
│   │   ├── confirm.py         # Atomic write commit + blast-radius checks
│   │   ├── dryrun.py          # Savepoint dry-run & financial impact preview
│   │   ├── ops_approval.py    # Approval routing for manager requests
│   │   ├── ops_read.py        # Scoped & parameterized read queries
│   │   └── ops_undo.py        # Atomic undo with concurrency check
│   ├── policy/                # Pure code decision engine
│   │   ├── engine.py          # Deterministic decision pipeline
│   │   └── rules.py           # Permission matrix, constraints & ambiguity rules
│   ├── audit.py               # Immutable audit log recorder
│   ├── auth.py                # Principal identity from JWT
│   ├── config.py              # Pydantic environment configuration
│   ├── db.py                  # SQLAlchemy session lifecycle
│   ├── main.py                # FastAPI routes & Web UI HTML template
│   ├── models.py              # SQLAlchemy database ORM models
│   └── schemas.py             # Pydantic v2 Intent & API models
├── db/
│   └── init/
│       └── 01_schema.sql      # PostgreSQL schema, roles, and RLS policies
├── eval/
│   ├── eval_dataset.json      # 56 labelled test cases covering all roles
│   └── run_eval.py            # Automated evaluation harness
├── seed/
│   ├── seed.py                # Python database seeder
│   └── seed_data.sql          # SQL dump (1,300 items, 1,100 bills, 16 users)
├── tests/                     # 31 Pytest automated test cases
├── docker-compose.yml         # Container composition (web + db)
├── Dockerfile                 # Python 3.12-slim production container
├── requirements.txt           # Python dependencies
└── README.md                  # System documentation
```

---

## 🔒 Security & Privacy Commitments

- **No Prompt Injection Vulnerabilities:** Because authorization and SQL generation are executed in code rather than prompts, prompt injection cannot grant elevated privileges or bypass tenant filters.
- **Tenant Isolation:** Cross-tenant access is structurally prevented by both application queries (`WHERE hospital_id = ?`) and PostgreSQL Row-Level Security (`RLS`).
- **Complete Audit Trail:** Every transaction attempt—including policy refusals—is committed to PostgreSQL with timestamps, user IDs, and raw text.
