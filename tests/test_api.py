from app.tools.memory_tools import memory_bank


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["agent_mode"] == "heuristic"


def test_dashboard_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "CLOUDFINOPS SENTINEL" in res.text


def test_state_payload_shape(client):
    body = client.get("/api/state").json()
    for key in ("kpis", "approvals", "remediations", "all_resources", "charts", "events"):
        assert key in body
    assert body["data_source"] == "simulated"
    kpis = body["kpis"]
    assert kpis["resources_monitored"] > 0
    assert 0 <= kpis["efficiency_score"] <= 100
    assert kpis["monthly_spend"] >= kpis["wasted_spend"]


def test_audit_creates_actions_and_run_record(client):
    result = client.post("/api/audit").json()
    assert result["status"] == "success"
    assert result["mode"] == "heuristic"
    assert result["anomalies_found"] > 0

    runs = memory_bank.snapshot()["runs"]
    assert runs[-1]["status"] == "SUCCESS"
    assert runs[-1]["finished_at"] is not None


def test_audit_is_idempotent(client):
    """A second audit must not re-remediate resources handled in the first."""
    client.post("/api/audit")
    first = len(memory_bank.snapshot()["remediations"])
    client.post("/api/audit")
    assert len(memory_bank.snapshot()["remediations"]) == first


def test_approval_flow(client):
    client.post("/api/audit")
    pending = memory_bank.pending_approvals()
    assert pending, "the heuristic audit should escalate at least one action"

    target = pending[0]
    before = memory_bank.total_savings()
    res = client.post(
        "/api/approvals", json={"resource_id": target["resource_id"], "status": "APPROVED"}
    )
    assert res.status_code == 200
    assert memory_bank.total_savings() > before


def test_rejecting_does_not_book_savings(client):
    client.post("/api/audit")
    target = memory_bank.pending_approvals()[0]
    before = memory_bank.total_savings()
    client.post(
        "/api/approvals", json={"resource_id": target["resource_id"], "status": "REJECTED"}
    )
    assert memory_bank.total_savings() == before


def test_unknown_approval_returns_404(client):
    res = client.post("/api/approvals", json={"resource_id": "nope", "status": "APPROVED"})
    assert res.status_code == 404


def test_invalid_status_is_rejected(client):
    res = client.post("/api/approvals", json={"resource_id": "x", "status": "MAYBE"})
    assert res.status_code == 422


def test_trigger_and_webhook(client):
    assert client.post("/api/trigger").json()["status"] == "initiated"
    # The webhook is a separate credential from the operator session.
    response = client.post(
        "/webhook/pubsub", json={"message": {}},
        headers={"X-Sentinel-Token": "test-token"},
    )
    assert response.json()["status"] == "accepted"


def test_preflight_endpoint(client):
    body = client.get("/api/preflight").json()
    assert "checks" in body and "ready" in body
    assert body["project_id"]


def test_state_exposes_the_safety_gate(client):
    body = client.get("/api/state").json()
    assert body["dry_run"] is True
    assert body["writes_enabled"] is False
    assert body["metrics_source"] in ("monitoring", "modelled")


def test_health_reports_mode(client):
    body = client.get("/health").json()
    assert body["dry_run"] is True
    assert body["mock_mode"] is True


# --- Lazy startup ---------------------------------------------------------
def test_nothing_is_scanned_until_the_user_asks(client):
    """Startup and dashboard polling must not touch GCP."""
    from app.tools import gcp_inventory, gcp_metrics

    # reset(), not clear(): the dashboard deliberately keeps the last scan
    # after its freshness window lapses.
    gcp_metrics._services_cache.reset()
    gcp_metrics._utilization_cache.reset()
    gcp_inventory._discovery_cache.reset()

    body = client.get("/api/state").json()
    assert body["scanned"] is False
    assert body["data_source"] == "idle"
    assert body["inventory"] == []
    assert body["kpis"]["resources_monitored"] == 0


def test_state_polling_never_triggers_discovery(client, monkeypatch):
    from app.tools import gcp_inventory, gcp_metrics

    gcp_metrics._services_cache.reset()
    gcp_inventory._discovery_cache.reset()

    def explode(*a, **k):
        raise AssertionError("polling /api/state must not start a scan")

    monkeypatch.setattr(gcp_inventory, "discover_cloud_run", explode)
    for _ in range(3):
        client.get("/api/state")


def test_audit_populates_the_inventory(client):
    from app.tools import gcp_inventory, gcp_metrics

    gcp_metrics._services_cache.reset()
    gcp_metrics._utilization_cache.reset()
    gcp_inventory._discovery_cache.reset()

    assert client.get("/api/state").json()["scanned"] is False
    client.post("/api/audit")
    body = client.get("/api/state").json()
    assert body["scanned"] is True
    assert body["inventory"]


# --- Trace ----------------------------------------------------------------
def test_trace_endpoint_returns_steps(client):
    client.post("/api/audit")
    steps = client.get("/api/trace").json()["steps"]
    assert steps
    phases = {s["phase"] for s in steps}
    assert "ANALYSIS" in phases


def test_trace_since_paginates(client):
    client.post("/api/audit")
    all_steps = client.get("/api/trace").json()["steps"]
    last = all_steps[-1]["seq"]
    assert client.get(f"/api/trace?since={last}").json()["steps"] == []


def test_approval_produces_an_execution_trace(client):
    """The whole point: see the action dispatched and its outcome."""
    from app.tools.memory_tools import memory_bank

    client.post("/api/audit")
    pending = memory_bank.pending_approvals()
    assert pending

    target = pending[0]["resource_id"]
    before = client.get("/api/trace").json()["steps"][-1]["seq"]
    client.post("/api/approvals", json={"resource_id": target, "status": "APPROVED"})

    new_steps = client.get(f"/api/trace?since={before}").json()["steps"]
    phases = [s["phase"] for s in new_steps]
    assert "APPROVAL" in phases, "the human decision must be recorded"
    assert "EXECUTION" in phases, "the dispatched action must be recorded"

    executions = [s for s in new_steps if s["phase"] == "EXECUTION"]
    assert any(s["detail"] for s in executions), "execution steps must carry payloads"


def test_scan_result_survives_its_freshness_window(client):
    """Regression: the TTL governed *freshness*, but also decided whether we had
    data at all — so 60s after a scan the dashboard reverted to 'NOT SCANNED'
    with zeroed KPIs while approvals from that scan were still on screen."""
    from app.tools import gcp_inventory, gcp_metrics

    client.post("/api/audit")
    before = client.get("/api/state").json()
    assert before["scanned"] is True and before["kpis"]["resources_monitored"] > 0

    # Simulate the freshness window lapsing.
    gcp_metrics._services_cache.clear()
    gcp_metrics._utilization_cache.clear()
    gcp_inventory._discovery_cache.clear()

    after = client.get("/api/state").json()
    assert after["scanned"] is True, "a lapsed cache must not read as 'never scanned'"
    assert after["data_source"] == before["data_source"]
    assert after["kpis"]["resources_monitored"] == before["kpis"]["resources_monitored"]
    assert after["scanned_age_seconds"] is not None


def test_reset_really_forgets(client):
    client.post("/api/audit")
    assert client.get("/api/state").json()["scanned"] is True
    client.post("/api/reset")
    assert client.get("/api/state").json()["scanned"] is False


def test_the_test_suite_can_never_write_to_real_infrastructure():
    """A guard on the guard: if this fails, every other test is unsafe."""
    from app.core.config import settings

    assert settings.MOCK_MODE is True
    assert settings.DRY_RUN is True
    assert settings.writes_enabled is False


def test_an_approval_pushes_a_state_change(client, monkeypatch):
    """Approving must reach open dashboards immediately, not on the next poll."""
    from app.core import trace as trace_mod
    from app.tools.memory_tools import memory_bank

    pushed = []
    monkeypatch.setattr(trace_mod.tracer, "notify_state_changed",
                        lambda reason="": pushed.append(reason))

    client.post("/api/audit")
    target = memory_bank.pending_approvals()[0]["resource_id"]
    client.post("/api/approvals", json={"resource_id": target, "status": "APPROVED"})

    assert any(p.startswith("decision:") for p in pushed)
    assert any(p.startswith("executed:") for p in pushed)


def test_a_rejection_pushes_but_does_not_execute(client, monkeypatch):
    from app.core import trace as trace_mod
    from app.tools.memory_tools import memory_bank

    pushed = []
    monkeypatch.setattr(trace_mod.tracer, "notify_state_changed",
                        lambda reason="": pushed.append(reason))

    client.post("/api/audit")
    target = memory_bank.pending_approvals()[0]["resource_id"]
    client.post("/api/approvals", json={"resource_id": target, "status": "REJECTED"})

    assert any(p.startswith("decision:") for p in pushed)
    assert not any(p.startswith("executed:") for p in pushed)


def test_the_suite_is_hermetic():
    """A judge cloning the repo must get a green run with no credentials,
    no .env and no network."""
    from app.core.config import settings

    assert settings.PROJECT_ID, "tests must not depend on a discovered key file"
    assert settings.MOCK_MODE is True
    assert settings.GEMINI_API_KEY == ""


def test_an_executed_action_refreshes_the_inventory(client, monkeypatch):
    """Regression: after approving, the SSE push arrived but /api/state still
    served the cached inventory — so costs, states and charts stayed stale
    until the next scan."""
    from app.main import refresh_inventory
    from app.tools import gcp_inventory, gcp_metrics

    calls = []
    original = gcp_inventory.discover_cloud_run
    monkeypatch.setattr(
        gcp_inventory, "discover_cloud_run",
        lambda: (calls.append(1), original())[1],
    )

    gcp_metrics._services_cache.reset()
    gcp_inventory._discovery_cache.reset()

    gcp_metrics.describe_resources()
    after_first = len(calls)
    gcp_metrics.describe_resources()
    assert len(calls) == after_first, "a cached read must not hit GCP"

    refresh_inventory()
    assert len(calls) == after_first + 1, (
        "a post-action refresh must re-read GCP exactly once, not zero or twice"
    )

    _, source = gcp_metrics.describe_resources(allow_discovery=False)
    assert source == "gcp", "the dashboard must now see fresh data"
