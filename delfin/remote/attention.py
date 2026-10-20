"""A cluster that needs a login, as an item in the user's attention list.

``ask_for_login`` records a ``cluster_login`` item when a cluster tool finds
no live tunnel: the window lists it under NEEDS YOU and opens its masked
login field for it (never a text answer, which would go to the model). One
pending item per cluster: asking again while one is open adds none.
``logged_in`` resolves a cluster's items once a login opened the tunnel,
without an answer and acknowledged, so nothing of it is handed to a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union


def ask_for_login(cluster: str, *, reason: str = "", session_id: str = "",
                  workspace: Optional[Union[str, Path]] = None) -> Optional[str]:
    """The id of the cluster's pending login item, recorded now if there was
    none; None when the inbox cannot be written."""
    from delfin.agent import attention
    try:
        for event in attention.list_pending("cluster_login"):
            if event.get("cluster") == cluster:
                return str(event.get("id"))
        return attention.emit_attention(
            "cluster_login", cluster=cluster, session_id=session_id, workspace=workspace,
            title=f"Log in to {cluster}",
            detail=reason or f"A cluster tool needs a login to {cluster}.")
    except Exception:
        return None


def logged_in(cluster: str) -> int:
    """Resolve the cluster's pending login items; how many there were."""
    from delfin.agent import attention
    done = 0
    try:
        for event in attention.list_pending("cluster_login"):
            if event.get("cluster") == cluster and attention.resolve(
                    str(event.get("id")), answer=None, acknowledged=True):
                done += 1
    except Exception:
        pass
    return done
