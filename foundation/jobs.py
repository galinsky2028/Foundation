"""Background jobs, run by one worker thread inside the web process.

Register a handler and queue work:

    from foundation.jobs import job, enqueue

    @job("send_digest")
    def send_digest(payload, log):
        log(f"sending to {payload['to']}")

    enqueue("send_digest", {"to": "a@b.c"})

Register work that repeats:

    from foundation.jobs import every

    @every(hours=24)
    def nightly_cleanup(log): ...

Jobs are claimed with a conditional UPDATE, so each runs once even if two processes run a
worker. A failed job is retried up to 3 times with growing delays.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import timedelta
from typing import Callable

from sqlalchemy import select, update

from .config import settings
from .db import SessionLocal, aware, utcnow
from .models import Job

log = logging.getLogger(__name__)

HANDLERS: dict[str, Callable] = {}
PERIODIC: list[dict] = []
MAX_ATTEMPTS = 3
_stop = threading.Event()
_thread: threading.Thread | None = None


def job(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


def every(seconds: int = 0, minutes: int = 0, hours: int = 0):
    interval = seconds + minutes * 60 + hours * 3600

    def deco(fn):
        PERIODIC.append({"fn": fn, "interval": interval, "last": 0.0})
        return fn

    return deco


def enqueue(kind: str, payload: dict | None = None, delay_seconds: int = 0) -> int:
    with SessionLocal() as s:
        j = Job(kind=kind, payload=payload or {}, run_after=utcnow() + timedelta(seconds=delay_seconds))
        s.add(j)
        s.commit()
        return j.id


def claim_next() -> int | None:
    with SessionLocal() as s:
        ids = s.scalars(select(Job.id).where(Job.status == "queued", Job.run_after <= utcnow())
                        .order_by(Job.id).limit(5)).all()
        for jid in ids:
            res = s.execute(update(Job).where(Job.id == jid, Job.status == "queued")
                            .values(status="running", started_at=utcnow(), attempts=Job.attempts + 1))
            s.commit()
            if res.rowcount == 1:
                return jid
    return None


def run(job_id: int) -> None:
    with SessionLocal() as s:
        j = s.get(Job, job_id)

        def log_fn(line: str) -> None:
            j.log = (j.log or "") + f"[{utcnow():%H:%M:%S}] {line}\n"
            s.commit()

        try:
            handler = HANDLERS.get(j.kind)
            if handler is None:
                raise RuntimeError(f"No handler registered for job kind '{j.kind}'")
            handler(j.payload or {}, log_fn)
            j.status = "done"
        except Exception as exc:
            j.error = f"{exc}\n{traceback.format_exc()}"
            if j.attempts < MAX_ATTEMPTS:
                j.status = "queued"
                j.run_after = utcnow() + timedelta(seconds=60 * 4 ** j.attempts)
            else:
                j.status = "failed"
            log.exception("job %s (%s) failed", job_id, j.kind)
        finally:
            j.finished_at = utcnow()
            s.commit()


def recover_interrupted() -> None:
    """At start-up, jobs left 'running' by a stopped process are queued again."""
    with SessionLocal() as s:
        for j in s.scalars(select(Job).where(Job.status == "running")).all():
            j.status = "queued"
            j.log = (j.log or "") + "[restart] re-queued after an interrupted run\n"
        s.commit()


def run_periodic() -> None:
    now = time.time()
    for p in PERIODIC:
        if now - p["last"] >= p["interval"]:
            p["last"] = now
            try:
                p["fn"](lambda line, n=p["fn"].__name__: log.info("%s: %s", n, line))
            except Exception:
                log.exception("periodic task %s failed", p["fn"].__name__)


def _loop() -> None:
    while not _stop.is_set():
        jid = claim_next()
        if jid:
            run(jid)
            continue
        run_periodic()
        _stop.wait(2.0)


def start() -> None:
    global _thread
    if not settings.run_worker or (_thread and _thread.is_alive()):
        return
    recover_interrupted()
    for p in PERIODIC:
        p["last"] = time.time()  # first run one interval after start
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="foundation-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def job_age(j: Job) -> float:
    return (utcnow() - aware(j.created_at)).total_seconds()
