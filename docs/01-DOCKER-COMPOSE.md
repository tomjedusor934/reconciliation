# Docker Compose Configuration Guide

## Overview
The `docker-compose.yml` defines the entire stack:
- **Backend** (FastAPI)
- **Frontend** (Vue 3 + Vite)
- **Database** (PostgreSQL 16)
- **Cache** (Redis)
- **Airflow** (optional, under `profiles: ["airflow"]`)

## docker-compose.yml Structure

### Network
```yaml
networks:
  reconcil-network:
    driver: bridge
```
Single bridge network. All services can resolve each other by container name (e.g., `postgres://db:5432` from backend).

### Services

#### 1. `db` (PostgreSQL 16)
```yaml
db:
  image: postgres:16-alpine
  container_name: reconciliation-db
  environment:
    POSTGRES_USER: reconciliation_user
    POSTGRES_PASSWORD: <from .env>
    POSTGRES_DB: reconciliation_db
  volumes:
    - db_data:/var/lib/postgresql/data
  ports:
    - "5432:5432"  # Host access
  networks:
    - reconcil-network
```
**Key points:**
- Internal hostname: `db` (used by backend via `DATABASE_URL=postgresql://user:pass@db:5432/...`)
- External port 5432 for dev tools (psql, DBeaver)
- Data persisted in `db_data` volume
- `init_db.py` runs on backend startup, creates schemas, seeds superadmin

#### 2. `redis` (Redis 7)
```yaml
redis:
  image: redis:7-alpine
  container_name: reconciliation-redis
  ports:
    - "6379:6379"
  networks:
    - reconcil-network
```
**Key points:**
- Internal: `redis://redis:6379`
- DB 0: session cache (backend)
- DB 1: Celery broker (Airflow workers)
- Used by Airflow for `celery_broker_connection_uri`

#### 3. `backend` (FastAPI)
```yaml
backend:
  build:
    context: ./backend
    dockerfile: Dockerfile
  container_name: reconciliation-backend
  environment:
    DATABASE_URL: postgresql://reconciliation_user:...@db:5432/reconciliation_db
    REDIS_URL: redis://redis:6379/0
    BACKEND_PORT: 8000
    INBOX_BASE_PATH: /shared/inbox
    AIRFLOW_API_URL: http://airflow-webserver:8080/api/v1
    RECO_ARCHIVE_AFTER_DAYS: 90
    SECRET_KEY: <from .env>
    ALGORITHM: HS256
  volumes:
    - ./backend:/app/backend
    - ./shared/inbox:/shared/inbox
    - ./shared/inbox_processed:/shared/inbox_processed
    - ./shared/inbox_error:/shared/inbox_error
  ports:
    - "8000:8000"
  depends_on:
    - db
    - redis
  networks:
    - reconcil-network
```
**Key points:**
- Mounts `./shared/inbox/` to `/shared/inbox` (for polling)
- `init_db.py` and `init_reco.py` run on startup
- `seed_flows.py` seeds the 5 flows
- Services run on 0.0.0.0:8000
- Depends on db + redis (docker compose waits for them, but doesn't wait for DB to be ready; backend retries)

#### 4. `frontend` (Vue 3 + Vite)
```yaml
frontend:
  build:
    context: ./frontend
    dockerfile: Dockerfile
  container_name: reconciliation-frontend
  environment:
    VITE_API_URL: http://localhost:8000/api/v1  # Browser → backend
  volumes:
    - ./frontend:/app/frontend
    - /app/frontend/node_modules  # Persist node_modules
  ports:
    - "5173:5173"
  depends_on:
    - backend
  networks:
    - reconcil-network
```
**Key points:**
- Dev server on 0.0.0.0:5173
- `VITE_API_URL` is for browser requests (localhost:8000)
- Frontend polls `http://localhost:8000/api/v1` from the browser
- Hot reload via mounted source

#### 5. `airflow-db` (PostgreSQL 16, for Airflow metadata)
```yaml
airflow-db:
  image: postgres:16-alpine
  profiles: ["airflow"]
  container_name: airflow-db
  environment:
    POSTGRES_USER: airflow
    POSTGRES_PASSWORD: <from .env>
    POSTGRES_DB: airflow
  volumes:
    - airflow_db_data:/var/lib/postgresql/data
  networks:
    - reconcil-network
```
**Key points:**
- Separate DB for Airflow metadata (connections, DAGs, runs, logs)
- Only created if `--profile airflow` is used

#### 6. `airflow-webserver`
```yaml
airflow-webserver:
  image: apache/airflow:2.8.1-python3.11
  profiles: ["airflow"]
  container_name: airflow-webserver
  environment:
    AIRFLOW_HOME: /opt/airflow
    AIRFLOW__CORE__DAGS_FOLDER: /opt/airflow/dags
    AIRFLOW__CORE__EXECUTOR: CeleryExecutor
    AIRFLOW__CELERY__BROKER_URL: redis://redis:6379/1
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql://airflow:...@airflow-db/airflow
    # + other Airflow configs
  volumes:
    - ./shared/dags:/opt/airflow/dags
    - ./shared/airflow_logs:/opt/airflow/logs
    - ./shared/airflow_plugins:/opt/airflow/plugins
  ports:
    - "8080:8080"
  depends_on:
    - airflow-db
    - redis
  networks:
    - reconcil-network
  entrypoint: >
    bash -c "airflow db init &&
             airflow users create ... &&
             airflow webserver"
```
**Key points:**
- UI on http://localhost:8080
- Reads DAGs from `/opt/airflow/dags` (mounted from `./shared/dags/`)
- Stores metadata in `airflow-db`
- Uses Redis (db 1) as Celery broker
- Entrypoint initializes DB and creates admin user

#### 7. `airflow-scheduler`
```yaml
airflow-scheduler:
  image: apache/airflow:2.8.1-python3.11
  profiles: ["airflow"]
  container_name: airflow-scheduler
  environment:
    # Same as webserver
  volumes:
    - ./shared/dags:/opt/airflow/dags
    - ./shared/airflow_logs:/opt/airflow/logs
  depends_on:
    - airflow-db
    - redis
  networks:
    - reconcil-network
  entrypoint: airflow scheduler
```
**Key points:**
- Schedules tasks based on DAG definitions
- Monitors DAGs and triggers based on schedules

#### 8. `airflow-worker` (Celery)
```yaml
airflow-worker:
  image: apache/airflow:2.8.1-python3.11
  profiles: ["airflow"]
  container_name: airflow-worker
  environment:
    # Same as webserver/scheduler
  volumes:
    - ./shared/dags:/opt/airflow/dags
    - ./shared/airflow_logs:/opt/airflow/logs
  depends_on:
    - airflow-db
    - redis
  networks:
    - reconcil-network
  entrypoint: airflow celery worker --queues=default
```
**Key points:**
- Executes tasks pushed to Celery queue
- Pulls from Redis (db 1)

### Volumes

```yaml
volumes:
  db_data:           # PostgreSQL main DB data
  airflow_db_data:   # Airflow metadata DB data
```

## Startup Flows

### Basic Stack (No Airflow)
```bash
docker-compose up -d
```
- Starts: db, redis, backend, frontend
- Backend initializes DB, seeds roles/users/flows on first run
- Frontend dev server ready on :5173
- Backend API ready on :8000

### Full Stack with Airflow
```bash
docker-compose up -d
docker-compose --profile airflow up -d
```
- First command: starts basic stack (as above)
- Second command: starts airflow-db, airflow-webserver, airflow-scheduler, airflow-worker
- Airflow reads DAGs from `./shared/dags/`

### External Airflow (No Containers)
Keep `AIRFLOW_USE_EXTERNAL=true` in `.env`. Airflow runs on your corporate server.
Backend calls it via `AIRFLOW_API_URL`, passing `AIRFLOW_USER` + `AIRFLOW_PASSWORD`.

## Networking Deep Dive

### Inside the Container Network
Containers resolve by service name:
- **Backend** → DB: `postgresql://reconciliation_user:...@db:5432/reconciliation_db`
- **Backend** → Redis: `redis://redis:6379/0`
- **Backend** → Airflow: `http://airflow-webserver:8080/api/v1`
- **Airflow scheduler** → Redis: `redis://redis:6379/1`

### From the Host (Your Machine)
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`
- Backend API: `localhost:8000`
- Frontend: `localhost:5173`
- Airflow UI: `localhost:8080`

## Environment Variables

Create a `.env` file (template in `.env.example`):

```bash
# Database
POSTGRES_USER=reconciliation_user
POSTGRES_PASSWORD=secure_password
POSTGRES_DB=reconciliation_db

# Redis
REDIS_URL=redis://redis:6379/0

# Backend
BACKEND_PORT=8000
BACKEND_CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
BACKEND_DEBUG=false

# Paths (inside backend container)
INBOX_BASE_PATH=/shared/inbox
RECO_ARCHIVE_AFTER_DAYS=90

# Airflow
AIRFLOW_USE_EXTERNAL=false
AIRFLOW_API_URL=http://airflow-webserver:8080/api/v1
AIRFLOW_USER=airflow
AIRFLOW_PASSWORD=airflow_password

# Auth
SECRET_KEY=your-secret-key-change-in-production
ALGORITHM=HS256
SSO_DEFAULT_ROLE_NAME=superadmin
```

## Health Checks & Debugging

### Check if containers are running
```bash
docker-compose ps
```

### View logs
```bash
docker-compose logs -f backend         # Follow backend logs
docker-compose logs db                  # DB logs
docker-compose logs --profile airflow -f airflow-webserver
```

### Connect to database
```bash
docker exec -it reconciliation-db psql -U reconciliation_user -d reconciliation_db
```

### Test backend health
```bash
curl http://localhost:8000/api/v1/health  # If endpoint exists
curl http://localhost:8000/docs            # Swagger UI
```

### Restart a service
```bash
docker-compose restart backend
docker-compose restart db
```

### Rebuild and restart
```bash
docker-compose up -d --build backend
```

## Important Notes

1. **DB initialization happens once** — the `init_db.py` script runs on backend startup and is idempotent (checks if superadmin role exists before creating).

2. **Inbox folders** — must exist on host before starting. Backend container mounts them. If they don't exist, create:
   ```bash
   mkdir -p shared/inbox/{atm,mt940_ip,mt940_other,webripost,finacle}
   mkdir -p shared/inbox_processed/{atm,mt940_ip,mt940_other,webripost,finacle}
   mkdir -p shared/inbox_error/{atm,mt940_ip,mt940_other,webripost,finacle}
   ```

3. **Frontend VITE_API_URL** — is the browser-side API URL. Must point to the backend's external address (usually `localhost:8000` for local dev).

4. **Airflow connections** — created via Airflow UI (`/admin/connections/`) or Airflow CLI. The backend's `POST /connections/test` endpoint tests them without logging.

5. **SSO configuration** — set `sso_force=true` and `sso_create_account_on_login=true` via `/admin/settings/` UI after first login.

6. **Volumes are preserved** — data in `db_data` and `airflow_db_data` persists even after `docker-compose down`. To reset:
   ```bash
   docker-compose down -v   # -v removes named volumes
   ```

---

## Performance Tuning (When Needed)

- **PostgreSQL**: Increase `shared_buffers`, `effective_cache_size` if running on powerful hardware
- **Redis**: Already lightweight, adjust `maxmemory-policy` if cache grows
- **Airflow**: Increase parallelism in `airflow.cfg` if more tasks run concurrently
- **Backend**: Increase `WORKERS=4` or more if CPU-bound
