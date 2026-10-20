"""What the /clusters and /jobs screens get from the service: grouped, cached, clean.

Input: ``delfin.agent.service.clusters_feed`` with its sources replaced: the
agent's background view, DELFIN's job monitor, the registry and the
connection (fakes that count their probes). Output: the dicts the service
answers ``jobs.list`` and ``clusters.list`` with.

Semantics: the groups come as agent work per session, then the job
monitor, then each logged-in cluster's active jobs (with the number that
finished); a source that cannot be read is named in errors; a cluster is
asked over its tunnel at most once per probe interval; every string is one
line of printable text -- a job name from a cluster cannot carry an escape
sequence, a newline or a bidi override into the terminal; a login error
never carries the password or the OTP, whatever its text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from delfin.agent.service import clusters_feed as feed
from delfin.remote import errors


@dataclass
class Job:
    jobid: str
    name: str
    state: str = "RUNNING"
    source: str = "active"
    partition: str = "small"
    elapsed: str = "1:00"
    timelimit: str = "2:00:00"
    nodes: str = "1"


@dataclass(frozen=True)
class Cluster:
    name: str
    usable: bool = True
    target: str = "me@login.example"
    problems: tuple = ()
    requires_password: bool = True
    requires_otp: bool = False
    work_dir: str = "~/delfin/jobs"
    default_partition: str | None = "small"


@dataclass
class Conn:
    live: bool = True
    listing: list = field(default_factory=list)
    calls: dict = field(default_factory=lambda: {"jobs": 0, "budget": 0})

    def is_live(self):
        return self.live

    def jobs(self):
        self.calls["jobs"] += 1
        return self.listing

    def budget(self):
        self.calls["budget"] += 1
        from cluster_tunnel.api import Decision
        return Decision(allowed=True, reason="", used=2.5, limit=100.0, unit="jobh")


@pytest.fixture
def world(monkeypatch):
    feed.forget()
    from delfin.agent import background_view, job_monitor
    from delfin.remote import clusters as registry
    from delfin.remote import connection
    conns = {"kcist": Conn(listing=[Job("51901", "\x1b[2Jgq_prior\nrm -rf ~"),
                                    Job("52000", "done", state="COMPLETED", source="finished")])}
    monkeypatch.setattr(registry, "load", lambda settings=None: {
        "kcist": Cluster("kcist"), "horeka": Cluster("horeka", usable=False,
                                                     problems=("not pinned",))})
    monkeypatch.setattr(connection, "connect", lambda name, settings=None: conns[name])
    monkeypatch.setattr(background_view, "collect", lambda ws, session_id="": {"ws": ws})
    monkeypatch.setattr(background_view, "rows", lambda view: [
        {"id": "b1", "kind": "Shell", "label": "pytest -q‮", "detail": "running · 2 min"}])
    monkeypatch.setattr(job_monitor, "load_watched", lambda: {"jobs": {
        "4711": {"folder": "/scratch/run1", "last_state": "PENDING"}}})
    yield conns
    feed.forget()


def test_the_groups_are_agent_work_then_the_monitor_then_each_cluster(world):
    got = feed.jobs([{"sessionId": "s1", "workspace": "/ws/a", "title": "benzene"},
                     {"sessionId": "s2", "workspace": "", "title": "no workspace"}])
    assert [g["source"] for g in got["groups"]] == ["agent", "monitor", "cluster"]
    agent, monitor, cluster = got["groups"]
    assert agent["title"] == "benzene" and agent["rows"][0]["label"] == "pytest -q"
    assert monitor["rows"][0] == {"id": "4711", "kind": "SLURM", "label": "/scratch/run1",
                                  "state": "PENDING", "detail": "watched by DELFIN's job monitor"}
    assert cluster["cluster"] == "kcist" and cluster["finished"] == 1
    (row,) = cluster["rows"]
    assert row["id"] == "51901" and row["state"] == "RUNNING"
    assert got["errors"] == []


def test_no_control_character_reaches_the_window(world):
    got = feed.jobs([{"sessionId": "s1", "workspace": "/ws/a", "title": "t\x07\x1b]0;x"}])
    text = repr(got)
    for bad in ("\x1b", "\n", "\x07", "‮"):
        assert bad not in text, repr(bad)
    label = got["groups"][-1]["rows"][0]["label"]
    assert label == "[2Jgq_prior rm -rf ~"


def test_a_source_that_cannot_be_read_is_named(world, monkeypatch):
    from delfin.agent import job_monitor

    def broken():
        raise OSError("watch file unreadable")

    monkeypatch.setattr(job_monitor, "load_watched", broken)
    got = feed.jobs([])
    assert [g["source"] for g in got["groups"]] == ["cluster"]
    assert any("job monitor" in e and "unreadable" in e for e in got["errors"])


def test_a_cluster_is_asked_over_its_tunnel_once_per_interval(world):
    conn = world["kcist"]
    first = feed.clusters()["clusters"]
    feed.clusters()
    feed.jobs([])
    feed.jobs([])
    assert conn.calls == {"jobs": 2, "budget": 1}   # one row probe, one /jobs probe
    row = next(r for r in first if r["name"] == "kcist")
    assert row["state"] == "logged_in" and row["jobs"] == 1
    assert row["budget"] == {"used": 2.5, "limit": 100.0, "unit": "jobh"}
    refused = next(r for r in first if r["name"] == "horeka")
    assert refused["state"] == "refused" and refused["problems"] == ["not pinned"]
    feed.forget("kcist")
    feed.clusters()
    assert conn.calls["budget"] == 2


def test_a_login_error_never_carries_the_secrets(monkeypatch):
    from delfin.remote import connection

    class Talkative:
        def login(self, password, otp, timeout=120):
            raise errors.LoginFailed(f"ssh said: {password} / {otp} rejected")

    monkeypatch.setattr(connection, "connect", lambda name, settings=None: Talkative())
    got = feed.login("kcist", "hunter2-pw", "424242")
    assert (got["ok"], got["code"]) == (False, "login_failed")
    assert "hunter2-pw" not in got["error"] and "424242" not in got["error"]
    assert got["error"] == "ssh said: *** / *** rejected"
