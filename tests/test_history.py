"""Per-scan history: what each run found, proposed, and what became of it."""

from app.tools.memory_tools import build_history, memory_bank


def test_history_is_empty_before_any_scan():
    assert build_history() == []


def test_each_scan_carries_its_own_recommendations(client):
    client.post("/api/audit")
    history = build_history()

    assert len(history) == 1
    entry = history[0]
    assert entry["index"] == 1
    assert entry["status"] == "SUCCESS"
    assert entry["approvals"], "the scan raised recommendations"
    assert all(a["run_id"] == entry["run_id"] for a in entry["approvals"])


def test_history_records_the_human_decisions(client):
    client.post("/api/audit")
    pending = memory_bank.pending_approvals()
    approved, rejected = pending[0]["resource_id"], pending[1]["resource_id"]

    client.post("/api/approvals", json={"resource_id": approved, "status": "APPROVED"})
    client.post("/api/approvals", json={"resource_id": rejected, "status": "REJECTED"})

    entry = build_history()[0]
    assert entry["counts"]["approved"] == 1
    assert entry["counts"]["rejected"] == 1
    assert entry["savings"]["realized"] > 0

    statuses = {a["resource_id"]: a["status"] for a in entry["approvals"]}
    assert statuses[approved] == "APPROVED"
    assert statuses[rejected] == "REJECTED"

    # Executions cover both the human-approved action and any Level 1 actions
    # the agent applied on its own during the same scan.
    executed = {r["resource_id"] for r in entry["remediations"]}
    assert approved in executed
    assert rejected not in executed, "a rejected recommendation must never run"


def test_execution_is_attributed_to_the_scan_that_proposed_it(client):
    """The human approves after the run finished; the outcome still belongs to
    the scan that raised the ticket."""
    client.post("/api/audit")
    entry = build_history()[0]
    target = entry["approvals"][0]["resource_id"]

    client.post("/api/approvals", json={"resource_id": target, "status": "APPROVED"})

    entry = build_history()[0]
    assert entry["remediations"], "the execution must appear under its own scan"
    assert entry["remediations"][0]["run_id"] == entry["run_id"]


def test_newest_scan_comes_first(client):
    client.post("/api/audit")
    client.post("/api/audit")
    history = build_history()
    assert [h["index"] for h in history] == [2, 1]


def test_a_rejected_recommendation_is_not_raised_again(client):
    """A human said no. Re-proposing it every scan is alert fatigue."""
    client.post("/api/audit")
    target = memory_bank.pending_approvals()[0]["resource_id"]
    client.post("/api/approvals", json={"resource_id": target, "status": "REJECTED"})

    client.post("/api/audit")
    second = build_history()[0]
    assert target not in {a["resource_id"] for a in second["approvals"]}


def test_history_endpoint_is_localised(client):
    client.post("/api/audit")
    es = client.get("/api/history?lang=es").json()["history"][0]
    en = client.get("/api/history?lang=en").json()["history"][0]
    assert es["approvals"][0]["proposed_action"] != en["approvals"][0]["proposed_action"]
    assert es["savings"] == en["savings"], "figures must not change with language"


# --- Scan-to-scan comparison ---------------------------------------------
def test_first_scan_has_nothing_to_compare(client):
    client.post("/api/audit")
    assert build_history()[0]["changes"]["first_scan"] is True


def test_an_unchanged_estate_says_so_explicitly(client):
    """Two identical scans must be distinguishable from a broken one."""
    client.post("/api/audit")
    client.post("/api/audit")

    changes = build_history()[0]["changes"]
    assert changes["first_scan"] is False
    assert changes["changed"] == []
    assert changes["added"] == []
    assert changes["unchanged"] > 0, "it must report how many stayed identical"


def test_the_snapshot_is_a_resource_fingerprint(client):
    """Regression: a local name collision stored the memory-bank dump here."""
    client.post("/api/audit")
    snapshot = memory_bank.snapshot()["runs"][-1]["snapshot"]

    assert "remediations" not in snapshot, "this must be resources, not bank state"
    first = next(iter(snapshot.values()))
    assert set(first) == {"status", "cost", "waste", "cpu", "memory"}


def test_status_and_cost_changes_are_reported():
    from app.tools.memory_tools import diff_snapshots

    before = {"svc": {"status": "Idle", "cost": 10.0, "waste": 5.0, "cpu": 2.0, "memory": 3.0}}
    after = {"svc": {"status": "Healthy", "cost": 4.0, "waste": 0.0, "cpu": 40.0, "memory": 3.0}}

    d = diff_snapshots(before, after)
    deltas = d["changed"][0]["deltas"]
    assert deltas["status"] == ["Idle", "Healthy"]
    assert deltas["cost"] == [10.0, 4.0]
    assert deltas["cpu"] == [2.0, 40.0]
    assert "memory" not in deltas, "a sub-threshold move is not a change"


def test_added_and_removed_resources_are_tracked():
    from app.tools.memory_tools import diff_snapshots

    base = {"status": "Idle", "cost": 1.0, "waste": 1.0, "cpu": 1.0, "memory": 1.0}
    d = diff_snapshots({"gone": base}, {"fresh": base})
    assert d["added"] == ["fresh"]
    assert d["removed"] == ["gone"]


def test_the_snapshot_covers_every_resource_type(client):
    """A newly orphaned disk must register as a change, not go unnoticed."""
    client.post("/api/audit")
    snapshot = memory_bank.snapshot()["runs"][-1]["snapshot"]
    statuses = {v["status"] for v in snapshot.values()}

    from app.tools.gcp_metrics import describe_resources

    cloud_run_only = {r["resource_id"] for r in describe_resources()[0]}
    assert set(snapshot) >= cloud_run_only
    assert statuses - {"Healthy", "Idle", "Oversized"}, (
        "non-Cloud-Run resources must appear in the fingerprint"
    )
