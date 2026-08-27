"""In-process job registry for the RCP reattribution tool.

WHY. Both halves of the tool are batches, not requests: analysing the 2026-08-13
extract sweeps ~266k live entries for 1 627 message ids and then round-trips to
the datamart, and committing N movements is N database transactions. Held in one
HTTP request, that ran past nginx's ``proxy_read_timeout`` and the operator got a
504 with no idea whether anything had happened. So the request now only STARTS
the work and returns an id; the browser polls a cheap status endpoint. No proxy
timeout can bite a request that answers in milliseconds, and the operator sees
which phase is running instead of a spinner.

Deliberately in-process and in-memory rather than Redis: the backend runs as a
SINGLE uvicorn worker (``CMD ["uvicorn", "app.main:app", …]`` with no
``--workers``), so one dict is enough, and ``app/main.py`` already runs a daemon
thread for the Airflow sync — this is the same shape.

The registry holds the LIVE view (phase, counters, log tail). What must outlive
the browser — the run and its report — is written to ``reco.rcp_run``: an
operator has no reason to sit in front of a batch, and a finished analysis used
to vanish the moment the page reloaded. The two are complementary, so a poll
falls back to the table once the registry has forgotten.

The worker thread opens its OWN session: the request's session is closed the
moment the response is sent.
"""
import logging
import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.repositories.rcp_run_repository import rcp_run_repository

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# Finished jobs are kept so a page refresh can still read the result, then
# dropped — the report holds thousands of rows and this is a temporary tool.
JOB_TTL = timedelta(hours=2)
MAX_JOBS = 20
# Lines kept per job. The UI shows the tail; the rest is scrollback for when
# something went wrong and the operator wants to know where.
MAX_LOG_LINES = 300


@dataclass
class Job:
    id: str
    kind: str                      # "analyze" | "commit"
    status: str = STATUS_RUNNING
    phase: str = ""                # human-readable, shown as-is in the UI
    done: int = 0
    total: int = 0
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    def public(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": list(self.logs),
        }


class Progress:
    """What a running job reports. Handed to the worker function, which calls it
    at every phase change — the only coupling between the batch and the UI.

    ``__call__`` sets the current phase (one value, overwritten); ``log`` appends
    a line that stays. A batch that only says "running" tells the operator
    nothing when it takes twenty minutes: the log is where it says WHAT it found.
    """

    def __init__(self, job: Job, lock: threading.Lock) -> None:
        self._job = job
        self._lock = lock

    def __call__(self, phase: str, done: int = 0, total: int = 0) -> None:
        with self._lock:
            self._job.phase = phase
            self._job.done = done
            self._job.total = total

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            self._job.logs.append(f"{stamp}  {message}")


class RcpJobService:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(
        self,
        kind: str,
        work: Callable[[Session, Progress], Dict[str, Any]],
        *,
        user_id: Optional[int] = None,
        label: str = "",
    ) -> Job:
        """Run ``work(db, progress)`` in a thread; return the job immediately.

        The run is recorded in ``reco.rcp_run`` before the thread starts and
        updated when it ends, so it is findable even if the browser never comes
        back — that is the whole point of not blocking the operator.
        """
        job = Job(id=str(uuid.uuid4()), kind=kind)
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        progress = Progress(job, self._lock)
        rcp_run_repository.open_run(
            run_id=job.id, kind=kind, status=STATUS_RUNNING, user_id=user_id,
            label=label, started_at=job.started_at,
        )

        def run() -> None:
            db = SessionLocal()
            try:
                result = work(db, progress)
                with self._lock:
                    job.result = result
                    job.status = STATUS_DONE
                    job.phase = "terminé"
                progress.log("terminé")
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                logger.exception("[rcp-job] %s %s failed", kind, job.id)
                with self._lock:
                    job.status = STATUS_ERROR
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.phase = "échec"
                progress.log(f"ÉCHEC — {type(exc).__name__}: {exc}")
                logger.debug("[rcp-job] traceback: %s", traceback.format_exc())
            finally:
                db.close()
                with self._lock:
                    job.finished_at = datetime.now(timezone.utc)
                    snapshot = (job.status, job.phase, job.error, job.result, job.finished_at)
                rcp_run_repository.close_run(
                    run_id=job.id, status=snapshot[0], phase=snapshot[1],
                    error=snapshot[2], result=snapshot[3], finished_at=snapshot[4],
                )
                rcp_run_repository.prune()

        threading.Thread(target=run, name=f"rcp-{kind}-{job.id[:8]}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def _prune_locked(self) -> None:
        """Drop finished jobs past their TTL, then the oldest ones over the cap.
        A running job is never dropped."""
        now = datetime.now(timezone.utc)
        for job_id, job in list(self._jobs.items()):
            if job.finished_at and now - job.finished_at > JOB_TTL:
                self._jobs.pop(job_id, None)
        if len(self._jobs) <= MAX_JOBS:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status != STATUS_RUNNING),
            key=lambda j: j.finished_at or j.started_at,
        )
        for job in finished[: len(self._jobs) - MAX_JOBS]:
            self._jobs.pop(job.id, None)


rcp_job_service = RcpJobService()
