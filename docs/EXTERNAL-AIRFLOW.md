# Intégration d'un Airflow Externe

Ce guide explique comment connecter l'application de réconciliation à une instance Airflow déjà existante (Airflow corporate, cluster dédié, etc.) au lieu d'utiliser le Airflow embarqué dans le `docker-compose.yml`.

---

## Prérequis

- L'Airflow externe doit pouvoir atteindre le backend de réconciliation via HTTP
- Les DAGs doivent être déployés sur l'Airflow externe
- L'API REST Airflow doit être activée avec `basic_auth`

---

## Étape 1 — Configurer le `.env`

```bash
# Activer le mode Airflow externe
AIRFLOW_USE_EXTERNAL=true

# URL de l'API REST de votre Airflow
AIRFLOW_API_URL="http://votre-airflow:8080/api/v1"

# Credentials Airflow (utilisateur avec droits Admin pour trigger les DAGs)
AIRFLOW_API_USER="airflow"
AIRFLOW_API_PASSWORD="votre-mot-de-passe"
```

> **Note** : le profil `["airflow"]` du `docker-compose.yml` principal n'est PAS démarré quand `AIRFLOW_USE_EXTERNAL=true`. Seuls `db`, `redis`, `backend` et `frontend` sont nécessaires.

---

## Étape 2 — Déployer les DAGs

Copier le contenu de `shared/dags/` vers le `dags_folder` de votre Airflow externe :

```bash
cp shared/dags/*.py /chemin/vers/airflow/dags/
```

**Fichiers nécessaires :**

| Fichier | Rôle |
|---------|------|
| `common.py` | Client HTTP pour appeler le backend (utilise `RECO_BACKEND_URL`) |
| `dag_factory.py` | Factory pour les DAGs d'ingestion |
| `ingest_atm.py` | DAG ingestion ATM (*/15 min) |
| `ingest_ip.py` | DAG ingestion IP (*/30 min, paused) |
| `ingest_webripost.py` | DAG ingestion Webripost (*/30 min, paused) |
| `ingest_float_ip.py` | DAG ingestion Float IP (@daily, paused) |
| `ingest_other_payments.py` | DAG ingestion Other Payments (*/30 min, paused) |
| `reconcile_daily.py` | Réconciliation automatique (02:00 UTC) |
| `archive_matched.py` | Émargement sweep (03:30 UTC) |
| `orchestrate_ingestion.py` | Orchestrateur (trigger externe) |

---

## Étape 3 — Configurer les variables d'environnement Airflow

Sur votre Airflow externe, définir ces variables d'environnement (ou via l'UI Airflow → Admin → Variables) :

```bash
RECO_BACKEND_URL=http://backend-host:8000    # URL du backend accessible depuis Airflow
RECO_BACKEND_INTERNAL_TOKEN=votre-token      # Token partagé (doit correspondre au .env du backend)
```

> **Important** : `RECO_BACKEND_URL` doit pointer vers le backend tel que vu depuis le réseau Airflow. Si les deux sont sur le même réseau Docker, utilisez le nom de service (`http://backend:8000`). Sinon, utilisez l'URL publique du backend.

---

## Étape 4 — Connectivité réseau

### Cas 1 : Même réseau Docker

Si l'Airflow externe est sur la même machine Docker, joignez le réseau de l'app :

```yaml
# Dans le compose Airflow externe
networks:
  reco-app-network:
    external: true
    name: reconciliation_orchestro-network
```

### Cas 2 : Serveurs séparés

Si Airflow est sur un serveur différent :
- Le backend doit être exposé sur un port accessible (ex: via reverse proxy)
- `RECO_BACKEND_URL` doit utiliser l'URL publique du backend
- Le token `RECO_BACKEND_INTERNAL_TOKEN` doit être partagé de manière sécurisée

---

## Étape 5 — Tester la connexion

### 1. Vérifier la santé Airflow depuis le backend

```bash
curl -u airflow:password http://votre-airflow:8080/api/v1/health
```

### 2. Vérifier que les DAGs sont chargés

```bash
curl -u airflow:password http://votre-airflow:8080/api/v1/dags | python3 -c "
import json,sys
for d in json.load(sys.stdin)['dags']:
    print(f'{d[\"dag_id\"]:30s} paused={d[\"is_paused\"]}')"
```

### 3. Tester l'ingestion bout en bout

```bash
cd scripts/
bash test_e2e_orchestrator.sh 100 100 100 0.5 0.7 0.6
```

### 4. Trigger manuel de l'orchestrateur

```bash
curl -X POST http://votre-airflow:8080/api/v1/dags/orchestrate_ingestion/dagRuns \
  -H "Content-Type: application/json" \
  -u "airflow:password" \
  -d '{"conf": {}}'
```

---

## Simulation locale : `docker-compose.airflow.yml`

Pour tester le mode Airflow externe en local, un fichier `docker-compose.airflow.yml` est fourni. Il crée un Airflow autonome qui se connecte au réseau de l'app principale.

### Démarrage

```bash
# 1. Démarrer l'app principale
docker compose up -d

# 2. Démarrer l'Airflow externe
docker compose -f docker-compose.airflow.yml up -d

# 3. Vérifier
docker compose -f docker-compose.airflow.yml ps
curl -u airflow:airflow http://localhost:8081/api/v1/health
```

### Arrêt

```bash
docker compose -f docker-compose.airflow.yml down
```

### UI Airflow

Accessible sur **http://localhost:8081** (port configurable via `AIRFLOW_EXT_PORT` dans `.env`).

---

## Résumé des variables

| Variable | Où | Usage |
|----------|-----|-------|
| `AIRFLOW_USE_EXTERNAL` | `.env` backend | Active le mode client externe |
| `AIRFLOW_API_URL` | `.env` backend | URL de l'API Airflow |
| `AIRFLOW_API_USER` | `.env` backend | Username Airflow |
| `AIRFLOW_API_PASSWORD` | `.env` backend | Password Airflow |
| `RECO_BACKEND_URL` | Env Airflow | URL du backend vu depuis Airflow |
| `RECO_BACKEND_INTERNAL_TOKEN` | Les deux | Token partagé pour les endpoints `/tasks/*` |
| `AIRFLOW_EXT_PORT` | `.env` | Port du webserver Airflow externe local (défaut: 8081) |
