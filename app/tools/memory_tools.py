"""Memory Bank: the agent's persistent state.

Keeps approvals, remediations, activity events and audit runs. State is
persisted to a JSON file so a Cloud Run restart does not wipe the agent's
history (which is what prevents it from re-remediating the same resource in a
loop).
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.i18n import DEFAULT_LANG, t
from app.tools.state_store import build_store

logger = logging.getLogger(__name__)

MAX_EVENTS = 200
MAX_RUNS = 50


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Sentinel so `state_file=None` can mean "in memory only" rather than silently
# falling back to the configured path — a trap for tests and scripts.
_USE_SETTINGS = object()


class MemoryBank:
    def __init__(self, state_file: Any = _USE_SETTINGS, backend: Optional[str] = None):
        self._lock = threading.RLock()
        self.state_file = settings.STATE_FILE if state_file is _USE_SETTINGS else state_file
        self.store = build_store(
            backend or ("none" if self.state_file is None else settings.STATE_BACKEND),
            settings.PROJECT_ID,
            self.state_file,
        )
        self.data: Dict[str, List[Dict[str, Any]]] = {
            "remediations": [],
            "approvals": [],
            "events": [],
            "runs": [],
        }
        # Set while an audit is running so tickets and remediations can be
        # attributed to the scan that produced them.
        self.current_run_id: Optional[str] = None
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        data = self.store.load()
        if not data:
            return
        for key in self.data:
            if isinstance(data.get(key), list):
                self.data[key] = data[key]

    def _persist(self) -> None:
        self.store.save(self.data)

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------
    def log_event(
        self,
        message: str = "",
        level: str = "INFO",
        actor: str = "sentinel",
        resource_id: Optional[str] = None,
        key: Optional[str] = None,
        **params: Any,
    ) -> Dict[str, Any]:
        """Record an activity event.

        Pass `key` plus placeholders instead of a literal `message` so the event
        can be rendered in any language later — the log is written once but read
        by operators who may not share a language.
        """
        event = {
            "timestamp": utcnow(),
            "level": level,
            "actor": actor,
            # Rendered in English for logs and any consumer that ignores i18n.
            "message": message or t(DEFAULT_LANG, key or "", **params),
            "key": key,
            "params": params or None,
            "resource_id": resource_id,
        }
        with self._lock:
            self.data["events"].append(event)
            # Keep the log bounded; the dashboard only renders the tail.
            del self.data["events"][:-MAX_EVENTS]
            self._persist()
        return event

    # ------------------------------------------------------------------
    # Remediations
    # ------------------------------------------------------------------
    def log_remediation(
        self,
        event_id: str,
        action: str,
        savings: float,
        resource_id: Optional[str] = None,
        source: str = "agent",
        applied: bool = True,
        action_key: str = "",
        run_id: Optional[str] = None,
        resource_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # `resource_id` used to be dropped here, which silently broke
        # check_history() and let the agent redo the same fix forever.
        record = {
            "event_id": event_id,
            "resource_id": resource_id or event_id.split("_", 1)[-1],
            "action": action,
            "savings": round(float(savings), 2),
            "source": source,
            # False when DRY_RUN kept us from touching live infrastructure.
            "applied": applied,
            # The shape the resource had when we acted. A later scan compares
            # against it: if the resource changed and is still wasteful, a new
            # action is legitimate rather than a duplicate.
            "resource_state": resource_state or {},
            # A human approval lands after its scan finished, so the ticket's
            # run is passed in explicitly to keep the history coherent.
            "run_id": run_id or self.current_run_id,
            "timestamp": utcnow(),
        }
        with self._lock:
            self.data["remediations"].append(record)
            self._persist()
        logger.info(
            "Remediation logged: %s on %s (savings $%.2f)",
            action,
            record["resource_id"],
            record["savings"],
        )
        self.log_event(
            key="ev.remediation_applied" if applied else "ev.remediation_simulated",
            action=action,
            action_key=action_key,
            resource=record["resource_id"],
            savings=f"{record['savings']:.2f}",
            level="ACTION",
            resource_id=record["resource_id"],
        )
        return record

    def check_history(self, resource_id: str) -> Dict[str, Any]:
        """Return the most recent remediation applied to a resource.

        The agent must call this before acting so it never remediates the same
        resource twice. Returns ``{"found": False}`` when the resource is new.
        """
        with self._lock:
            matches = [
                r for r in self.data["remediations"] if r.get("resource_id") == resource_id
            ]
        if not matches:
            return {"found": False, "resource_id": resource_id}
        last = matches[-1]
        return {
            "found": True,
            "resource_id": resource_id,
            "last_action": last["action"],
            "last_timestamp": last["timestamp"],
            "last_state": last.get("resource_state") or {},
            "applied": last.get("applied", True),
            "times_remediated": len(matches),
        }

    def total_savings(self) -> float:
        with self._lock:
            return round(sum(r.get("savings", 0.0) for r in self.data["remediations"]), 2)

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------
    def has_pending_approval(self, resource_id: str) -> bool:
        with self._lock:
            return any(
                a["resource_id"] == resource_id and a["status"] == "PENDING"
                for a in self.data["approvals"]
            )

    def add_approval(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        ticket.setdefault("ticket_id", f"tkt_{uuid.uuid4().hex[:8]}")
        ticket.setdefault("status", "PENDING")
        ticket.setdefault("created_at", utcnow())
        ticket.setdefault("run_id", self.current_run_id)
        with self._lock:
            self.data["approvals"].append(ticket)
            self._persist()
        self.log_event(
            key="ev.approval_requested",
            action=ticket["proposed_action"],
            action_key=ticket.get("action_key") or "",
            resource=ticket["resource_id"],
            level="APPROVAL",
            resource_id=ticket["resource_id"],
        )
        return ticket

    def resolve_approval(self, resource_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Mark the oldest pending ticket for a resource as approved/rejected."""
        with self._lock:
            for approval in self.data["approvals"]:
                if approval["resource_id"] == resource_id and approval["status"] == "PENDING":
                    approval["status"] = status
                    approval["resolved_at"] = utcnow()
                    self._persist()
                    return dict(approval)
        return None

    def last_rejection(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """The most recent ticket a human declined for this resource."""
        with self._lock:
            rejected = [
                a for a in self.data["approvals"]
                if a["resource_id"] == resource_id and a["status"] == "REJECTED"
            ]
        return rejected[-1] if rejected else None

    def pending_approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a for a in self.data["approvals"] if a["status"] == "PENDING"]

    # ------------------------------------------------------------------
    # Audit runs
    # ------------------------------------------------------------------
    def start_run(self) -> Dict[str, Any]:
        run = {
            "run_id": f"run_{uuid.uuid4().hex[:8]}",
            "started_at": utcnow(),
            "finished_at": None,
            "status": "RUNNING",
            "anomalies_found": 0,
            "actions_taken": 0,
            "summary": "",
            "error": None,
        }
        with self._lock:
            self.data["runs"].append(run)
            del self.data["runs"][:-MAX_RUNS]
            self.current_run_id = run["run_id"]
            self._persist()
        return run

    def finish_run(self, run_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            for run in self.data["runs"]:
                if run["run_id"] == run_id:
                    run.update(fields)
                    run["finished_at"] = utcnow()
                    if self.current_run_id == run_id:
                        self.current_run_id = None
                    self._persist()
                    return dict(run)
        return None

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            return json.loads(json.dumps(self.data))

    def reset(self) -> None:
        with self._lock:
            for key in self.data:
                self.data[key] = []
            self._persist()


def render_event(event: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Return a copy of an event with its message in the requested language.

    A param named `*_key` holds a catalogue key rather than literal text, so
    nested phrases (an action name inside an event sentence) translate too.
    """
    if not event.get("key"):
        return event  # legacy or free-form event; show it as written

    params = dict(event.get("params") or {})
    for name, value in list(params.items()):
        if name.endswith("_key") and value:
            params[name[:-4]] = t(lang, str(value))
    return {**event, "message": t(lang, event["key"], **params)}


def diff_snapshots(
    previous: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """What changed between two scans.

    Without this, two identical scans are indistinguishable from a broken one:
    the operator sees the same findings and cannot tell whether nothing changed
    or nothing was re-read.
    """
    if not current:
        return {"first_scan": previous is None, "changed": [], "added": [],
                "removed": [], "unchanged": 0}
    if not previous:
        return {"first_scan": True, "changed": [], "added": list(current),
                "removed": [], "unchanged": 0}

    added = [r for r in current if r not in previous]
    removed = [r for r in previous if r not in current]
    changed, unchanged = [], 0

    for rid, now in current.items():
        was = previous.get(rid)
        if was is None:
            continue
        deltas = {}
        if was["status"] != now["status"]:
            deltas["status"] = [was["status"], now["status"]]
        if abs(was["cost"] - now["cost"]) >= 0.01:
            deltas["cost"] = [was["cost"], now["cost"]]
        # Utilization moves constantly; only flag a meaningful shift.
        if abs(was["cpu"] - now["cpu"]) >= 5.0:
            deltas["cpu"] = [was["cpu"], now["cpu"]]
        if abs(was["memory"] - now["memory"]) >= 5.0:
            deltas["memory"] = [was["memory"], now["memory"]]

        if deltas:
            changed.append({"resource_id": rid, "deltas": deltas})
        else:
            unchanged += 1

    return {"first_scan": False, "added": added, "removed": removed,
            "changed": changed, "unchanged": unchanged}


def build_history(lang: str = DEFAULT_LANG, limit: int = 20) -> List[Dict[str, Any]]:
    """One entry per scan: what it found, proposed, and what became of it.

    Answers "what happened in scan N?" — the recommendations it raised, which a
    human approved or rejected, and which actions actually ran.
    """
    store = memory_bank.snapshot()
    by_run: Dict[Optional[str], Dict[str, List[Dict[str, Any]]]] = {}

    for ticket in store["approvals"]:
        by_run.setdefault(ticket.get("run_id"), {}).setdefault("approvals", []).append(ticket)
    for rem in store["remediations"]:
        by_run.setdefault(rem.get("run_id"), {}).setdefault("remediations", []).append(rem)

    history: List[Dict[str, Any]] = []
    runs = store["runs"]
    for index, run in enumerate(runs, start=1):
        previous_snapshot = runs[index - 2].get("snapshot") if index >= 2 else None
        buckets = by_run.get(run["run_id"], {})
        approvals = [render_approval(a, lang) for a in buckets.get("approvals", [])]
        remediations = buckets.get("remediations", [])

        decided = [a for a in approvals if a["status"] != "PENDING"]
        history.append(
            {
                "index": index,
                "run_id": run["run_id"],
                "started_at": run["started_at"],
                "finished_at": run.get("finished_at"),
                "status": run["status"],
                "mode": run.get("mode"),
                "degraded": run.get("degraded"),
                "error": run.get("error"),
                "anomalies_found": run.get("anomalies_found", 0),
                "summary": run.get("summary", ""),
                "changes": diff_snapshots(previous_snapshot, run.get("snapshot")),
                "approvals": approvals,
                "remediations": remediations,
                "counts": {
                    "proposed": len(approvals),
                    "approved": len([a for a in approvals if a["status"] == "APPROVED"]),
                    "rejected": len([a for a in approvals if a["status"] == "REJECTED"]),
                    "pending": len(approvals) - len(decided),
                    "executed": len(remediations),
                    "applied_for_real": len([r for r in remediations if r.get("applied")]),
                },
                "savings": {
                    "proposed": round(sum(a["estimated_roi"] for a in approvals), 2),
                    "realized": round(sum(r.get("savings", 0.0) for r in remediations), 2),
                },
            }
        )

    return list(reversed(history))[:limit]


def render_approval(ticket: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Return a copy of an approval ticket rendered in the requested language.

    Tickets outlive the audit that raised them, so their text is stored as keys
    plus structured data rather than as a finished sentence.
    """
    # The model's recommendation outranks a catalogue label: it explains the
    # specific change, which a generic phrase cannot.
    if ticket.get("model_recommendation"):
        out = dict(ticket)
        out["proposed_action"] = ticket["model_recommendation"]
        if ticket.get("reason_key"):
            out["detailed_reason"] = t(
                lang, ticket["reason_key"], **(ticket.get("reason_params") or {})
            )
        return out

    if not ticket.get("action_key"):
        return ticket  # legacy ticket; show it as written

    from app.tools.rationale import render_changes

    out = dict(ticket)
    specs = ticket.get("change_specs") or []
    changes = [c for c in render_changes(specs, lang) if "current" not in str(c)]

    action = t(lang, ticket["action_key"])
    if changes:
        action = f"{action}: {', '.join(changes)}"
    out["proposed_action"] = action

    if ticket.get("reason_key"):
        out["detailed_reason"] = t(lang, ticket["reason_key"], **(ticket.get("reason_params") or {}))
    return out


memory_bank = MemoryBank()


def check_remediation_history(resource_id: str) -> Dict[str, Any]:
    """Check whether a resource has already been remediated in a previous run.

    Call this BEFORE proposing or executing any action on a resource. If it
    returns ``found: true``, skip the resource entirely — acting again would
    duplicate work or start a remediation loop.

    Args:
        resource_id: The resource to look up, e.g. "billing-worker".

    Returns:
        ``{"found": false, ...}`` for a resource never touched before, or
        ``{"found": true, "last_action": ..., "last_timestamp": ...,
        "times_remediated": N}``.
    """
    # Deliberately a module-level function, not `memory_bank.check_history`.
    # google-genai deep-copies the tool list, and deep-copying a bound method
    # drags in `self` — including MemoryBank's RLock, which cannot be pickled.
    # Plain functions are copied by reference, so they pass through untouched.
    return memory_bank.check_history(resource_id)
