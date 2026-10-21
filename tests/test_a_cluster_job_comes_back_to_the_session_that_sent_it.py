"""The job watcher: an ended cluster job's results come back, and its session is told.

Input: a job DELFIN submitted (a record in remote_jobs.jsonl, as
delfin.remote.jobs.submit writes it) on a cluster reached through a fake
``ssh`` that runs every remote command line in a local home, where the job's
folder holds what a DELFIN run leaves; the cluster's job list replaced.
Output: what ``watch.tick`` reports, the files in the workspace, the
watcher's outcome, the inbox, and what the session's watch says.

Semantics pinned here:

* a job that ended comes back once: its key results into <job folder>/
  <cluster>-<id>/ (DELFIN_Data.json, DELFIN.txt, outputs, logs, the exit
  marker, also from a subfolder), never wave functions, densities or
  scratch, nor a file over the size limit -- left on the cluster and said;
* the outcome is judged on the fetched copy: a COMPLETED job whose run
  failed is failed, and a failed one carries signatures and job_fix's
  assessment; a notice goes to NEEDS YOU, with the session;
* a running job is followed, not fetched; one the probe no longer lists is
  asked for by id; without a login nothing is asked and one login item says
  what waits; a fetch that cannot be made is tried again, then reported;
* the session's cluster watch says nothing until the outcome is there, then
  says it once, with the results, in its next turn or its wake-up; it is
  not pruned after seven days while the job is followed; ``/jobs`` shows the
  state the watcher saw.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from delfin.agent import attention, background_view, job_monitor, job_wake
from delfin.remote import connection, jobs, slurm, watch

_FAKE_SSH = """\
#!{python}
import os, subprocess, sys
WITH_VALUE = set("bcDEeFIiJLlmOoPpQRSWw")
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    if args[i] == "-O" and args[i + 1] == "check":
        sys.exit(0 if os.path.exists(os.environ["FAKE_MASTER"]) else 255)
    i += 2 if len(args[i]) == 2 and args[i][1] in WITH_VALUE else 1
sys.exit(subprocess.run(["sh", "-c", " ".join(args[i + 1:])], cwd=os.environ["HOME"]).returncode)
"""

SID = "s-0001"
REMOTE = "delfin/jobs/20261021-160135-89f7-fe"


@dataclass
class Job:
    jobid: str
    state: str
    source: str = "done"
    name: str = "fe"


class World:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.ws = tmp / "ws"
        self.master = tmp / "master"
        self.listing: list[Job] = []
        self.fields: dict[str, dict] = {}

    @property
    def remote(self) -> Path:
        return self.home / REMOTE

    @property
    def results(self) -> Path:
        return self.ws / "fe" / "kcist-4242"

    def submit(self, job_id: str = "4242", **extra) -> dict:
        record = {"cluster": "kcist", "job_id": job_id, "name": "fe", "kind": "delfin",
                  "input": "", "remote_dir": f"~/{REMOTE}", "folder": str(self.ws / "fe"),
                  "workspace": str(self.ws), "session_id": SID, "commit": "c0ffee",
                  "submitted_at": time.time(), **extra}
        path = jobs._records_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        job_monitor.register_agent_job(
            self.ws, f"kcist:{job_id}", description="fe: DELFIN workflow on kcist",
            extra={"kind": "cluster", "cluster": "kcist", "remote_id": job_id,
                   "session_id": SID})
        return record

    def leave(self, files: dict[str, object]) -> None:
        for rel, content in files.items():
            path = self.remote / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, int):
                path.write_bytes(b"x" * content)
            else:
                path.write_text(str(content), encoding="utf-8")


DONE = {"DELFIN_Data.json": '{"status": "completed", "completed": true}\n',
        "DELFIN.txt": "DELFIN report\n", ".exit_code_4242": "0\n",
        "delfin_4242.out": "DELFIN WRAPPER COMPLETED (exit 0)\n",
        "delfin_run.log": "workflow finished\n", "CONTROL.txt": "PAL=2\n",
        "input.txt": "O 0 0 0\n", "initial.out": "...\n****ORCA TERMINATED NORMALLY****\n",
        "initial.xyz": "1\n\nO 0 0 0\n", "initial.gbw": 4096, "initial.densities": 4096,
        "initial.tmp": 10, "ox_step_1/ox_step_1.out": "****ORCA TERMINATED NORMALLY****\n",
        "IR_initial.png": 100}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    for d in (w.home, w.ws / "fe"):
        d.mkdir(parents=True)
    w.master.touch()
    ctun_dir = w.home / ".config" / "cluster-tunnel"
    ctun_dir.mkdir(parents=True)
    (ctun_dir / "config.yaml").write_text(
        "clusters:\n  kcist:\n    host: login.example\n    user: me\n"
        "    strict_host_key_checking: 'yes'\n"
        f"    known_hosts_file: {ctun_dir / 'known_hosts'}\n", encoding="utf-8")
    (w.home / ".delfin_settings.json").write_text(json.dumps({"remote": {"clusters": {
        "kcist": {"default_partition": "small"}}}}), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(_FAKE_SSH.format(python=sys.executable), encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    for key, value in (("HOME", w.home), ("XDG_CONFIG_HOME", w.home / ".config"),
                       ("XDG_CACHE_HOME", w.home / ".cache"), ("FAKE_MASTER", w.master),
                       ("PATH", f"{bin_dir}:/usr/bin:/bin")):
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("CTUN_CONFIG", raising=False)
    monkeypatch.setattr(connection.Connection, "jobs",
                        lambda self, since_minutes=None: list(w.listing))
    monkeypatch.setattr(slurm, "job_fields", lambda conn, job_id: w.fields.get(job_id, {}))
    monkeypatch.setattr(watch, "PROBE_EVERY_S", 0)
    return w


def _told(world) -> list[dict]:
    return job_monitor.check_agent_jobs(world.ws, session_id=SID)


def _notices(kind: str) -> list[dict]:
    return [e for e in attention.list_pending(kind)]


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

def test_an_ended_job_comes_back_once_with_its_key_results(world):
    world.submit()
    world.leave(DONE)
    world.listing = [Job("4242", "COMPLETED")]
    tick = watch.tick()
    (ended,) = tick.ended
    assert ended["ok"] and ended["status"] == "succeeded" and ended["exit_code"] == 0
    assert ended["results"] == "fe/kcist-4242" and ended["session_id"] == SID
    came = sorted(str(p.relative_to(world.results)) for p in world.results.rglob("*")
                  if p.is_file())
    assert came == sorted(["DELFIN_Data.json", "DELFIN.txt", ".exit_code_4242",
                           "delfin_4242.out", "delfin_run.log", "CONTROL.txt", "input.txt",
                           "initial.out", "initial.xyz", "ox_step_1/ox_step_1.out",
                           "IR_initial.png"])
    assert ended["files"] == len(came) and ended["left_on_cluster"] == 0
    assert watch.tick().ended == []                     # once
    assert watch.outcome("kcist", "4242")["results"] == "fe/kcist-4242"
    (notice,) = _notices("run_finished")
    assert notice["title"] == "Job kcist:4242 finished (COMPLETED)"
    assert notice["session_id"] == SID and "fe/kcist-4242" in notice["detail"]
    assert world.remote.is_dir()                        # the job folder stays


def test_a_running_job_is_followed_and_nothing_is_fetched(world):
    world.submit()
    world.leave(DONE)
    world.listing = [Job("4242", "RUNNING", source="active")]
    tick = watch.tick()
    assert tick.ended == [] and tick.following == 1 and not world.results.exists()
    assert watch.seen_state("kcist", "4242") == "RUNNING" and watch.followed("kcist", "4242")
    (row,) = background_view.collect(str(world.ws), session_id=SID)["watches"]
    assert row["kind"] == "cluster" and row["state"] == "RUNNING"
    assert _told(world) == []


def test_a_job_the_probe_no_longer_lists_is_asked_for_by_id(world):
    world.submit(submitted_at=time.time() - 3 * 86400)
    world.leave(DONE)
    world.fields["4242"] = {"JobState": "COMPLETED"}
    (ended,) = watch.tick().ended
    assert ended["state"] == "COMPLETED" and ended["ok"]


def test_a_completed_job_whose_run_failed_is_judged_failed(world):
    world.submit()
    world.leave({**DONE, ".exit_code_4242": "1\n",
                 "initial.out": "ORCA finished by error termination in SCF\n"})
    world.listing = [Job("4242", "COMPLETED")]
    (ended,) = watch.tick().ended
    assert not ended["ok"] and ended["status"] == "failed" and ended["exit_code"] == 1
    assert ended["assessment"]
    (notice,) = _notices("run_failed")
    assert notice["title"] == "Job kcist:4242 failed (COMPLETED)" and notice["session_id"] == SID


def test_a_failed_job_carries_its_signatures(world):
    world.submit()
    world.leave({**DONE, ".exit_code_4242": "137\n",
                 "delfin_4242.err": "slurmstepd: error: Detected 1 oom_kill event in "
                                    "StepId=4242.batch.\n"})
    world.listing = [Job("4242", "OUT_OF_MEMORY")]
    (ended,) = watch.tick().ended
    assert not ended["ok"] and ended["state"] == "OUT_OF_MEMORY"
    assert ended["signatures"] == ["out-of-memory"]


def test_files_over_the_limit_stay_on_the_cluster(world, monkeypatch):
    monkeypatch.setattr(watch, "FILE_BYTES", 50)
    world.submit()
    world.leave({**DONE, "initial.out": "o" * 200 + "\n****ORCA TERMINATED NORMALLY****\n"})
    world.listing = [Job("4242", "COMPLETED")]
    (ended,) = watch.tick().ended
    assert not (world.results / "initial.out").exists()
    assert ended["left_on_cluster"] >= 1 and "left on the cluster" in watch.summary(ended)


def test_without_a_login_nothing_is_asked_and_one_item_says_what_waits(world):
    world.submit()
    world.master.unlink()
    for _ in range(2):
        tick = watch.tick()
        assert tick.ended == [] and tick.waiting_for_login == ["kcist"]
    (item,) = [e for e in attention.list_pending("cluster_login") if e.get("cluster") == "kcist"]
    assert "1 DELFIN job(s) on kcist" in item["detail"]
    assert watch.followed("kcist", "4242") and watch.outcome("kcist", "4242") is None


def test_a_fetch_that_cannot_be_made_is_tried_again_then_reported(world):
    world.submit()                         # its folder is not on the cluster
    world.listing = [Job("4242", "COMPLETED")]
    for _ in range(watch.FETCH_TRIES - 1):
        assert watch.tick().ended == []
    (ended,) = watch.tick().ended
    assert ended["results"] == "" and "is gone on kcist" in ended["fetch_error"]
    assert "they stay on kcist" in watch.summary(ended)


def test_a_link_where_the_results_go_is_not_written_through(world, tmp_path):
    world.submit()
    world.leave(DONE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.results.symlink_to(elsewhere)
    world.listing = [Job("4242", "COMPLETED")]
    for _ in range(watch.FETCH_TRIES):
        watch.tick()
    assert list(elsewhere.iterdir()) == []
    assert "not a folder DELFIN made" in watch.outcome("kcist", "4242")["fetch_error"]


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def test_the_session_is_told_once_with_its_results(world):
    world.submit()
    world.leave(DONE)
    world.listing = [Job("4242", "RUNNING", source="active")]
    watch.tick()
    assert job_wake.finished_watched_jobs(str(world.ws), set(), session_id=SID) == []
    world.listing = [Job("4242", "COMPLETED")]
    watch.tick()
    woken = job_wake.finished_watched_jobs(str(world.ws), set(), session_id=SID)
    prompt = job_wake.wake_prompt(woken)
    assert "cluster kcist:4242 [COMPLETED]" in prompt and "results in fe/kcist-4242" in prompt
    assert job_wake.finished_watched_jobs(str(world.ws), set(), session_id=SID) == []
    assert _told(world) == []              # the wake told it; the turn retires the watch
    assert job_monitor.load_watched(job_monitor._agent_watch_path(world.ws))["jobs"] == {}


def test_a_turn_reads_it_when_nothing_woke_the_session(world):
    world.submit()
    world.leave(DONE)
    world.listing = [Job("4242", "COMPLETED")]
    watch.tick()
    (ev,) = _told(world)
    assert ev["kind"] == "cluster" and ev["ok"] and "results in fe/kcist-4242" in ev["results"]
    assert job_monitor.check_agent_jobs(world.ws, session_id="someone-else") == []


def test_a_cluster_watch_outlives_the_seven_day_prune_while_followed(world, monkeypatch):
    world.submit()
    path = job_monitor._agent_watch_path(world.ws)
    data = job_monitor.load_watched(path)
    data["jobs"]["kcist:4242"]["added_at"] = time.time() - 9 * 86400
    job_monitor.save_watched(data, path)
    assert _told(world) == [] and "kcist:4242" in job_monitor.load_watched(path)["jobs"]
    assert _notices("run_failed") == []
    world.leave(DONE)
    world.listing = [Job("4242", "COMPLETED")]
    watch.tick()
    (ev,) = _told(world)
    assert ev["job_id"] == "kcist:4242"
