"""DELFIN on a cluster, one install per commit: the PC's own, published, side by side.

Input: a ``Connection`` (``connection.connect``), the git checkout the PC
runs DELFIN from, and the install sources (``clusters.install_sources``).
Output: an ``Outcome`` per call; ``CommitNotPublished`` and ``InstallFailed``
(delfin.remote.errors) when DELFIN will not or could not install.

Semantics:

* The commit is the one the PC runs: HEAD of the checkout ``delfin`` is
  imported from. Changes in its working tree do not travel; the outcome
  notes them.
* DELFIN installs only a published commit unasked: one reachable from a
  branch of an install source (``git ls-remote``, then ancestry in the local
  repository, after fetching the branch heads' objects without writing a
  ref). Reachability matters: GitHub serves any commit of a fork network
  by its hash, so "the server knows this hash" would also admit a commit
  pushed only to somebody's fork.
* The tree travels over the tunnel as ``git archive`` of that commit into
  ``<install_root>/<sha>/``, with ``.delfin-commit`` naming commit and
  source. The cluster needs no access to GitHub for DELFIN itself, and gets
  exactly the commit, not the PC's working tree.
* ``install_delfin.sh`` runs there detached (``--repo`` that directory,
  ``--prefix`` the cluster's software_dir, the standard profile), so it
  outlives the tunnel and the PC. Each commit has its own venv; ORCA,
  OpenMPI and the tools are found or built once and shared. It writes
  ``~/.delfin_settings.json`` and ``~/.delfin_env.sh`` on the cluster as any
  install does.
* A complete install is used as it is; a running one is waited for; a
  failed one is reported with the tail of its log and started again only
  with ``retry=True``.
* ``remove_unused`` deletes DELFIN's installs (directories with
  ``.delfin-commit``, nothing else) that are not kept and that no queued or
  running job names (``--comment=delfin-commit=<sha>``, see ``job_comment``).
* Every upload, start and removal is written to the audit log.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import clusters as _clusters
from .errors import CommitNotPublished, InstallFailed

SCRIPT = Path(__file__).with_name("scripts") / "installs.sh"

#: How long ``ensure_installed`` waits for a first install (OpenMPI is
#: compiled, the tools are fetched) before it reports the install as
#: still running; the install itself carries on.
DEFAULT_WAIT_SECONDS = 3 * 3600


@dataclass(frozen=True)
class Commit:
    """The commit the PC runs DELFIN from."""

    sha: str
    checkout: Path
    dirty: bool  # tracked files differ from the commit; those changes do not travel


@dataclass
class Outcome:
    """What ``ensure_installed`` found or did."""

    sha: str
    path: str        # on the cluster, "~/..." or absolute
    state: str       # "complete" (present or installed now) | "running" (still going)
    installed_now: bool = False
    source: str = ""  # the install source the commit was found on
    notes: list[str] = field(default_factory=list)


def job_comment(sha: str) -> str:
    """The ``sbatch --comment`` that marks a job as running on install ``sha``,
    so ``remove_unused`` keeps that install while the job is queued or runs."""
    return f"delfin-commit={sha}"


def pc_commit(checkout: Optional[Path] = None) -> Commit:
    """The commit of the checkout DELFIN runs from (or of ``checkout``)."""
    if checkout is None:
        import delfin
        checkout = Path(delfin.__file__).resolve().parents[1]
    checkout = Path(checkout)
    try:
        sha = _git(checkout, "rev-parse", "HEAD").strip()
        status = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CommitNotPublished(
            f"DELFIN on this PC does not run from a git checkout ({checkout}), so there "
            f"is no commit to install on a cluster: {exc}") from exc
    return Commit(sha=sha, checkout=checkout, dirty=bool(status.strip()))


def published_source(commit: Commit, sources: Sequence[str]) -> Optional[str]:
    """The first install source on whose branches ``commit`` lies, or None."""
    for url in sources:
        try:
            listing = _git(commit.checkout, "ls-remote", "--heads", url, timeout=120)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        heads = sorted({line.split()[0] for line in listing.splitlines() if line.strip()})
        if commit.sha in heads:
            return url
        missing = [h for h in heads if not _has_commit(commit.checkout, h)]
        if missing:
            try:
                # Objects only: no ref and no FETCH_HEAD is written into the
                # user's repository.
                _git(commit.checkout, "fetch", "--quiet", "--no-tags",
                     "--no-write-fetch-head", url, *missing, timeout=600)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        for head in heads:
            if _has_commit(commit.checkout, head) and _is_ancestor(commit.checkout,
                                                                   commit.sha, head):
                return url
    return None


def state(conn, sha: str) -> str:
    """complete | running | failed | partial | absent, for install ``sha``."""
    return _script(conn, "state", _install_dir(conn, sha)).strip() or "absent"


def ensure_installed(conn, *, commit: Optional[Commit] = None,
                     sources: Optional[Sequence[str]] = None,
                     settings: Optional[dict] = None, retry: bool = False,
                     wait: bool = True, wait_seconds: float = DEFAULT_WAIT_SECONDS,
                     poll_seconds: float = 20,
                     on_progress: Optional[Callable[[str], None]] = None) -> Outcome:
    """The PC's commit installed on ``conn``'s cluster; installing it if needed."""
    say = on_progress or (lambda message: None)
    commit = commit or pc_commit()
    path = _install_dir(conn, commit.sha)
    notes = []
    if commit.dirty:
        notes.append(f"the PC's checkout has changes beyond commit {commit.sha[:10]}; "
                     "the cluster runs the commit without them")
    current = state(conn, commit.sha)
    if current == "complete":
        return Outcome(commit.sha, path, "complete", notes=notes)
    if current == "failed" and not retry:
        raise InstallFailed(f"the install of {commit.sha[:10]} on {conn.name} failed before; "
                            "fix the cause and install again with retry",
                            log=_script(conn, "log", path))
    source = ""
    if current != "running":
        if sources is None:
            sources = _clusters.install_sources(settings)
        say(f"checking that {commit.sha[:10]} is published ({', '.join(sources) or 'no source'})")
        source = published_source(commit, sources) or ""
        if not source:
            raise CommitNotPublished(
                f"commit {commit.sha[:10]} is on no branch of {', '.join(sources) or 'any source'}"
                "; push it there first, DELFIN installs only published commits on a cluster")
        say(f"uploading {commit.sha[:10]} to {conn.name}:{path}")
        _upload_tree(conn, commit, source, path)
        say(f"starting install_delfin.sh on {conn.name}")
        _script(conn, "start", path, conn.cluster.software_dir)
        _audit("install", conn.name, commit.sha, source, path)
    if not wait:
        return Outcome(commit.sha, path, "running", installed_now=True, source=source,
                       notes=notes)
    deadline = time.monotonic() + wait_seconds
    while True:
        current = state(conn, commit.sha)
        if current == "complete":
            say(f"{commit.sha[:10]} is installed on {conn.name}")
            return Outcome(commit.sha, path, "complete", installed_now=True, source=source,
                           notes=notes)
        if current in ("failed", "partial", "absent"):
            raise InstallFailed(f"the install of {commit.sha[:10]} on {conn.name} failed",
                                log=_script(conn, "log", path))
        if time.monotonic() > deadline:
            return Outcome(commit.sha, path, "running", installed_now=True, source=source,
                           notes=notes + ["still installing; it carries on without the PC"])
        say(f"installing {commit.sha[:10]} on {conn.name} ...")
        time.sleep(poll_seconds)


def installs(conn) -> dict[str, str]:
    """DELFIN's installs on the cluster: commit -> state."""
    out = {}
    for line in _script(conn, "list", conn.cluster.install_root).splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def in_use(conn) -> set[str]:
    """Commits that a queued or running job of the user's names."""
    return {line.strip() for line in _script(conn, "inuse").splitlines() if line.strip()}


def remove_unused(conn, keep: Sequence[str] = ()) -> list[str]:
    """Remove DELFIN's installs that are neither kept nor used by a job; the
    commits removed. A running install is never removed."""
    used = in_use(conn)
    removed = []
    for sha, st in installs(conn).items():
        if st == "running" or any(sha.startswith(k) or k.startswith(sha) for k in keep):
            continue
        if any(sha.startswith(u) or u.startswith(sha) for u in used):
            continue
        path = _install_dir(conn, sha)
        _script(conn, "remove", path)
        _audit("remove", conn.name, sha, "", path)
        removed.append(sha)
    return removed


# --- helpers ------------------------------------------------------------------------


def _install_dir(conn, sha: str) -> str:
    return f"{conn.cluster.install_root.rstrip('/')}/{sha}"


def _relative_to_home(path: str) -> str:
    """A remote path as rsync and ssh read it: relative paths start in the
    cluster's home, and "~" in an argument is not expanded."""
    return path[2:] if path.startswith("~/") else path


def _script(conn, action: str, *args: str) -> str:
    result = conn.feed_script(SCRIPT, [action, *args], timeout=600)
    if not result.ok:
        raise InstallFailed(f"{action} on {conn.name} failed (exit {result.exit_code}): "
                            f"{(result.stderr or '').strip()}")
    return result.stdout or ""


def _upload_tree(conn, commit: Commit, source: str, path: str) -> None:
    archive = subprocess.run(["git", "-C", str(commit.checkout), "archive", "--format=tar",
                              commit.sha], capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix="delfin-install-") as tmp:
        tree = Path(tmp) / commit.sha
        tree.mkdir()
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        (tree / ".delfin-commit").write_text(json.dumps({
            "commit": commit.sha, "source": source,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }) + "\n", encoding="utf-8")
        remote = _relative_to_home(path)
        parent = remote.rsplit("/", 1)[0] if "/" in remote else "."
        made = conn.run(["mkdir", "-p", parent])
        if not made.ok:
            raise InstallFailed(f"cannot create {parent} on {conn.name}: "
                                f"{(made.stderr or '').strip()}")
        result = conn.upload(f"{tree}/", f"{remote}/")
        if not result.ok:
            raise InstallFailed(f"uploading {commit.sha[:10]} to {conn.name} failed: "
                                f"{(result.stderr or '').strip()}")


def _git(checkout: Path, *args: str, timeout: float = 60) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                          text=True, encoding="utf-8", check=True, timeout=timeout).stdout


def _has_commit(checkout: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def _is_ancestor(checkout: Path, sha: str, head: str) -> bool:
    return subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", sha, head],
                          capture_output=True).returncode == 0


def _audit(action: str, cluster: str, sha: str, source: str, path: str) -> None:
    try:
        from delfin.agent import audit_log
        audit_log.append(audit_log.make_record(
            tool="cluster_install", decision="allow", mode=f"auto_{action}",
            path=path, reason=f"{action} DELFIN {sha[:10]} on {cluster}",
            extra={"cluster": cluster, "commit": sha, "source": source}))
    except Exception:
        pass  # the audit log never blocks the action it records (audit_log.append)


__all__ = ["Commit", "Outcome", "SCRIPT", "ensure_installed", "in_use", "installs",
           "job_comment", "pc_commit", "published_source", "remove_unused", "state"]
