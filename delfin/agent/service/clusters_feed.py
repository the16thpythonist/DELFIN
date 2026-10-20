"""The clusters and the jobs a window shows, and the login it hands to ctun.

Input: DELFIN's clusters (``delfin.remote.clusters``) and ctun's state; the
agent's background work per session workspace (``background_view``); the
SLURM jobs DELFIN's job monitor watches (``job_monitor.load_watched``).
Output: plain dicts for the service's ``clusters.list`` and ``jobs.list``
results; ``login`` and ``logout`` for ``cluster.login`` / ``cluster.logout``.
Runs in the supervisor, which calls it through ``asyncio.to_thread``.

Semantics:

* A cluster row: name, target, state -- ``refused`` (with its problems),
  ``logged_out`` or ``logged_in`` -- what a login asks for, and while logged
  in the session budget and the number of the user's active jobs. The login
  state is the local control socket; the budget and the job list are asked
  over the tunnel at most once a minute per cluster, since a window polls
  every few seconds.
* ``login``: the password and OTP go to ``delfin.remote.connection``, which
  hands them to ctun, and nowhere else. They are in no return value, log
  line or error text: a failure is reported by its code and a message that
  names neither (ctun's LoginError never does). The supervisor takes this
  request only from a window, never from an agent's process (``ACTING``),
  and never forwards it to a worker.
* ``jobs``: three kinds of group -- the agent's background shells, watches,
  sub-agents and wake-ups, one group per session; the jobs DELFIN's job
  monitor watches; the user's active jobs on each logged-in cluster, all of
  them (also those submitted by hand), asked at most twice a minute per
  cluster. A source that cannot be read is named in ``errors``, never shown
  as empty.
* Everything shown comes from places other processes write (a cluster's job
  names, a watch file in a workspace), so every string is cut to one line
  of printable characters: no escape sequence reaches the terminal.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Iterable, Optional

#: Seconds between two probes over a cluster's tunnel for its row (budget,
#: job count) and for /jobs (the job list).
ROW_PROBE_S = 60.0
JOBS_PROBE_S = 30.0

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def clean(value: Any, limit: int = 160) -> str:
    """``value`` as one line of printable text, at most ``limit`` characters."""
    text = _CONTROL.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cached(key: tuple[str, str], max_age: float, probe: Callable[[], Any],
            now: Optional[float] = None) -> Any:
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < max_age:
            return hit[1]
    value = probe()
    with _lock:
        _cache[key] = (now, value)
    return value


def forget(name: Optional[str] = None) -> None:
    """Drop the cached probes (of one cluster), after a login or logout."""
    with _lock:
        for key in [k for k in _cache if name is None or k[1] == name]:
            del _cache[key]


# --- clusters ------------------------------------------------------------------------


def clusters(*, settings: Optional[dict] = None) -> dict:
    """``{"clusters": [row, ...]}`` for every cluster in remote.clusters."""
    from delfin.remote import clusters as registry
    from delfin.remote import connection
    from delfin.remote.errors import RemoteError

    rows = []
    for name, cluster in registry.load(settings=settings).items():
        row = {"name": clean(name, 64), "target": clean(cluster.target, 120),
               "state": "refused", "problems": [clean(p, 300) for p in cluster.problems],
               "requiresPassword": bool(cluster.requires_password),
               "requiresOtp": bool(cluster.requires_otp),
               "workDir": clean(cluster.work_dir, 200),
               "defaultPartition": clean(cluster.default_partition or "", 64) or None,
               "budget": None, "jobs": None, "error": None}
        rows.append(row)
        if not cluster.usable:
            continue
        try:
            conn = connection.connect(name, settings=settings)
            live = conn.is_live()
        except RemoteError as exc:
            row["error"] = clean(f"{exc.code}: {exc}", 300)
            continue
        row["state"] = "logged_in" if live else "logged_out"
        if not live:
            continue
        try:
            probe = _cached(("row", name), ROW_PROBE_S, lambda: _row_probe(conn))
        except RemoteError as exc:
            row["error"] = clean(f"{exc.code}: {exc}", 300)
            continue
        row["budget"], row["jobs"] = probe
    return {"clusters": rows}


def _row_probe(conn) -> tuple[Optional[dict], int]:
    decision = conn.budget()
    budget = None
    if decision.limit is not None:
        budget = {"used": decision.used, "limit": decision.limit,
                  "unit": clean(decision.unit, 16)}
    active = [j for j in conn.jobs() if getattr(j, "source", "") == "active"]
    return budget, len(active)


def login(name: str, password: str, otp: str, *, settings: Optional[dict] = None,
          timeout: int = 120) -> dict:
    """``{"ok", "opened", "code", "error"}``; never the password or the OTP."""
    from delfin.remote import connection
    from delfin.remote.errors import RemoteError

    try:
        conn = connection.connect(name, settings=settings)
        opened = conn.login(password, otp, timeout=timeout)
    except RemoteError as exc:
        return {"ok": False, "opened": False, "code": exc.code,
                "error": _scrub(str(exc), password, otp)}
    except Exception as exc:  # noqa: BLE001 - the window gets a word, not a traceback
        return {"ok": False, "opened": False, "code": "internal_error",
                "error": _scrub(type(exc).__name__, password, otp)}
    finally:
        forget(name)
    from delfin.remote import attention as remote_attention
    remote_attention.logged_in(name)
    return {"ok": True, "opened": opened, "code": None, "error": None}


def logout(name: str, *, settings: Optional[dict] = None) -> dict:
    from delfin.remote import connection
    from delfin.remote.errors import RemoteError

    try:
        closed = connection.connect(name, settings=settings).logout()
    except RemoteError as exc:
        return {"ok": False, "closed": False, "code": exc.code, "error": clean(str(exc), 300)}
    finally:
        forget(name)
    return {"ok": True, "closed": closed, "code": None, "error": None}


def _scrub(text: str, *secrets: str) -> str:
    """``text`` cleaned, with any secret in it masked: ctun names none, and
    this keeps it so whatever a later release says."""
    out = clean(text, 300)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


# --- jobs ----------------------------------------------------------------------------


def jobs(sessions: Iterable[dict], *, settings: Optional[dict] = None) -> dict:
    """``{"groups": [...], "errors": [...]}`` for the /jobs screen.

    ``sessions``: the service's sessions, each with ``sessionId``,
    ``workspace`` and ``title``. A group: ``source`` (agent | monitor |
    cluster), ``title``, ``rows`` (each with ``id``, ``kind``, ``label``,
    ``state``, ``detail``) and, for a cluster, ``cluster``."""
    groups: list[dict] = []
    errors: list[str] = []
    try:
        from delfin.agent import background_view
        for s in sessions:
            workspace = str(s.get("workspace") or "")
            if not workspace:
                continue
            view = background_view.collect(workspace, session_id=str(s.get("sessionId") or ""))
            rows = [{"id": clean(r["id"], 64), "kind": clean(r["kind"], 40),
                     "label": clean(r["label"], 120), "state": "",
                     "detail": clean(r["detail"], 160)}
                    for r in background_view.rows(view)]
            if rows:
                groups.append({"source": "agent",
                               "title": clean(s.get("title") or workspace, 80),
                               "rows": rows})
    except Exception as exc:  # noqa: BLE001
        errors.append(clean(f"the agents' background work could not be read: {exc}", 200))
    try:
        from delfin.agent import job_monitor
        watched = job_monitor.load_watched().get("jobs") or {}
        rows = [{"id": clean(jid, 64), "kind": "SLURM",
                 "label": clean((rec or {}).get("folder") or jid, 120),
                 "state": clean((rec or {}).get("last_state") or "", 24),
                 "detail": "watched by DELFIN's job monitor"}
                for jid, rec in watched.items()]
        if rows:
            groups.append({"source": "monitor", "title": "DELFIN's job monitor", "rows": rows})
    except Exception as exc:  # noqa: BLE001
        errors.append(clean(f"the job monitor's list could not be read: {exc}", 200))
    groups += _cluster_groups(settings, errors)
    return {"groups": groups, "errors": errors}


def _cluster_groups(settings: Optional[dict], errors: list[str]) -> list[dict]:
    from delfin.remote import clusters as registry
    from delfin.remote import connection
    from delfin.remote.errors import RemoteError

    out = []
    try:
        listed = registry.load(settings=settings)
    except Exception as exc:  # noqa: BLE001
        errors.append(clean(f"the clusters could not be read: {exc}", 200))
        return out
    for name, cluster in listed.items():
        if not cluster.usable:
            continue
        try:
            conn = connection.connect(name, settings=settings)
            if not conn.is_live():
                continue
            listing = _cached(("jobs", name), JOBS_PROBE_S, conn.jobs)
        except RemoteError as exc:
            errors.append(clean(f"{name}: {exc.code}: {exc}", 200))
            continue
        rows = [{"id": clean(j.jobid, 32), "kind": clean(j.partition, 24),
                 "label": clean(j.name, 120), "state": clean(j.state, 24),
                 "detail": clean(f"{j.elapsed} of {j.timelimit} · {j.nodes} node(s)", 80)}
                for j in listing if getattr(j, "source", "") == "active"]
        finished = sum(1 for j in listing if getattr(j, "source", "") != "active")
        out.append({"source": "cluster", "cluster": clean(name, 64),
                    "title": clean(f"{name} ({cluster.target})", 120), "rows": rows,
                    "finished": finished})
    return out
