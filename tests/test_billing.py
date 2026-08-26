"""What Google charged, next to what the cost model estimated.

The rest of the agent prices the *allocation* — CPU and memory at Cloud Run
Tier-1 rates, assuming an always-on instance. That is the right signal for
right-sizing, and it is not an invoice. For a FinOps agent that gap is the most
obvious thing a reviewer challenges, so where the billing export exists the two
figures are shown side by side.

The behaviour that matters most here is the absence of the export, which is the
normal case: no table configured, a table that cannot be read, or a resource the
export cannot attribute. All three must leave the estimate standing alone rather
than inventing a billed figure, because "billed $0.00" is a much stronger claim
than "we did not look".
"""

from typing import Any, Dict, List

import pytest

from app.core.config import settings
from app.tools import gcp_billing

RESOURCE = {"resource_id": "checkout-api", "monthly_cost": 304.84}


class FakeRow(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class FakeQueryJob:
    def __init__(self, rows, error=None):
        self.rows = rows
        self.error = error

    def result(self, timeout=None):
        if self.error:
            raise self.error
        return self.rows


class FakeBQ:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.jobs: List[Dict[str, Any]] = []

    def query(self, query, job_config=None):
        self.jobs.append({"query": query, "config": job_config})
        return FakeQueryJob(self.rows, self.error)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_EXPORT_TABLE", "p.ds.gcp_billing_export_v1_X")
    monkeypatch.setattr(settings, "MOCK_MODE", False)


def use(monkeypatch, fake):
    monkeypatch.setattr(gcp_billing, "_client", lambda: fake)
    return fake


# --- 1. absence is the normal case, and must stay honest -------------------
def test_no_table_configured_means_no_claim():
    assert gcp_billing.is_configured() is False
    assert gcp_billing.fetch_billed_costs() is None


def test_an_unreadable_export_degrades_to_the_estimate(monkeypatch, configured):
    use(monkeypatch, FakeBQ(error=RuntimeError("403 Access Denied on table")))
    assert gcp_billing.fetch_billed_costs() is None


def test_none_and_empty_are_different_answers():
    """None is 'we could not look'; {} is 'we looked and there was nothing'.
    Only the first may make the UI stop showing billed figures."""
    assert gcp_billing.reconcile(RESOURCE, None) is None
    assert gcp_billing.reconcile(RESOURCE, {"costs": {}, "days_covered": 30}) is None


def test_an_unattributed_resource_shows_nothing_rather_than_zero(monkeypatch, configured):
    """Not every charge carries a label the export can map to a service.
    Reporting $0.00 there would claim the resource is free."""
    assert gcp_billing.reconcile(
        RESOURCE, {"costs": {"other-service": 12.0}, "days_covered": 30}
    ) is None


# --- 2. reading the export -------------------------------------------------
def test_credits_are_netted_off(monkeypatch, configured):
    """Credits are negative amounts in the export; ignoring them overstates
    the bill, which for a savings agent is the flattering direction."""
    use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="checkout-api", cost=200.0, credits=-50.0,
                                          window_start=None, window_end=None)]))
    assert gcp_billing.fetch_billed_costs()["costs"] == {"checkout-api": 150.0}


def test_a_missing_credit_column_is_not_a_crash(monkeypatch, configured):
    use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="a", cost=10.0, credits=None,
                                          window_start=None, window_end=None)]))
    assert gcp_billing.fetch_billed_costs()["costs"] == {"a": 10.0}


def test_the_query_is_bounded_by_bytes_and_project(monkeypatch, configured):
    """A runaway scan over a billing export is itself a cost incident."""
    fake = use(monkeypatch, FakeBQ(rows=[]))
    gcp_billing.fetch_billed_costs()

    config = fake.jobs[0]["config"]
    assert config.maximum_bytes_billed == settings.BILLING_MAX_BYTES
    names = {p.name for p in config.query_parameters}
    assert names == {"project", "days"}
    assert "usage_start_time >=" in fake.jobs[0]["query"], (
        "the date filter is what keeps a partitioned scan cheap"
    )


# --- 3. the comparison the operator actually reads -------------------------
def test_the_window_is_normalised_to_a_month(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_LOOKBACK_DAYS", 15)
    assert gcp_billing.to_monthly(100.0) == 200.0


def test_billed_under_estimate_is_reported_as_a_negative_delta(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_LOOKBACK_DAYS", 30)
    result = gcp_billing.reconcile(
        RESOURCE, {"costs": {"checkout-api": 198.11}, "days_covered": 30}
    )

    assert result["billed_monthly"] == 198.11
    assert result["estimated_monthly"] == 304.84
    assert result["delta"] == -106.73, (
        "a scale-to-zero service really being idle makes the estimate a ceiling; "
        "that is information, not an error"
    )
    assert result["source"] == "cloud_billing_export"


def test_billed_over_estimate_is_reported_too(monkeypatch):
    """Egress, CPU boost and per-request charges are not priced by the
    allocation model at all, so the invoice can exceed the estimate."""
    monkeypatch.setattr(settings, "BILLING_LOOKBACK_DAYS", 30)
    assert gcp_billing.reconcile(
        RESOURCE, {"costs": {"checkout-api": 420.0}, "days_covered": 30}
    )["delta"] == 115.16


# --- 4. it never runs where it should not ----------------------------------
def test_mock_mode_never_queries_billing(monkeypatch):
    """A simulated fleet has no invoice, and a demo must not spend money."""
    monkeypatch.setattr(settings, "BILLING_EXPORT_TABLE", "p.ds.t")
    monkeypatch.setattr(settings, "MOCK_MODE", True)
    fake = use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="a", cost=1.0, credits=0,
                                    window_start=None, window_end=None)]))

    from app.tools.gcp_metrics import billed_costs

    assert billed_costs() is None
    assert not fake.jobs


def test_preflight_says_which_cost_source_is_in_use():
    from app.tools.preflight import run_preflight

    check = [c for c in run_preflight()["checks"] if c["name"] == "Cost source"][0]
    assert check["status"] == "skip"
    assert "estimated" in check["detail"]
    assert "BILLING_EXPORT_TABLE" in check["fix"]


def test_the_suite_never_inherits_a_real_billing_table():
    """A test run must not issue BigQuery jobs against a real billing export.

    Settings are read from .env like everything else, so an operator who has
    configured the export would have `pytest` scan a partitioned table that is
    billed by bytes read. Cleared in conftest; asserted here so the clearing
    cannot quietly be dropped.
    """
    assert settings.BILLING_EXPORT_TABLE == ""


# --- 5. a young export must not be read as a month -------------------------
def test_a_days_old_export_is_scaled_by_what_it_actually_covers():
    """The export does not backfill. A day after enabling it holds a day.

    Scaling that by the configured 30-day window would report a thirtieth of the
    real bill next to the estimate — which reads as the cost model being wildly
    wrong, rather than the data being young.
    """
    result = gcp_billing.reconcile(
        {"resource_id": "a", "monthly_cost": 304.84},
        {"costs": {"a": 10.0}, "days_covered": 1.0},
    )
    assert result["billed_monthly"] == 300.0, "10/day is ~300/month, not ~10"
    assert result["partial"] is True
    assert result["window_days"] == 1.0


def test_a_full_window_is_not_marked_partial():
    result = gcp_billing.reconcile(
        {"resource_id": "a", "monthly_cost": 304.84},
        {"costs": {"a": 198.11}, "days_covered": 30.0},
    )
    assert result["partial"] is False
    assert result["billed_monthly"] == 198.11


def test_the_evidence_row_says_when_the_data_is_partial():
    """The operator has to see that a projection came from two days of rows."""
    from app.tools.rationale import explain

    resource = {
        "resource_id": "a", "type": "Cloud Run", "cpu_limit": "1",
        "memory_limit": "2Gi", "min_instances": 1, "cpu_utilization": 5.0,
        "memory_utilization": 6.0, "monthly_cost": 304.84, "wasted_cost": 200.0,
        "status": "Idle", "severity": "HIGH", "metrics_source": "monitoring",
        "billing": gcp_billing.reconcile(
            {"resource_id": "a", "monthly_cost": 304.84},
            {"costs": {"a": 10.0}, "days_covered": 1.0},
        ),
    }
    labels = [row["label"] for row in explain(resource)["evidence"]]
    assert any("partial" in label for label in labels), labels


# --- 6. a failure has to say which failure ---------------------------------
@pytest.mark.parametrize("error,expected", [
    ("404 Not found: Dataset p:billing_export was not found in location US",
     "not in the location"),
    ("403 BigQuery API has not been used in project 1 before or it is disabled",
     "not enabled"),
    ("403 Access Denied: Table p.d.t: User does not have permission",
     "cannot read the billing export"),
    ("404 Not found: Table p.d.t", "No such table"),
])
def test_each_failure_names_its_own_cause(monkeypatch, configured, error, expected):
    """One generic 'could not read' sends the operator to re-grant roles that
    were already correct. These are the three ways it actually fails, and each
    has a different fix."""
    use(monkeypatch, FakeBQ(error=RuntimeError(error)))

    from app.tools.preflight import _billing_check

    check = _billing_check()
    assert check["status"] == "fail"
    assert expected in check["detail"], check["detail"]
    assert check["fix"]


def test_an_empty_export_is_ok_and_says_it_is_still_filling(monkeypatch, configured):
    """A table with no rows yet is a real answer, not a failure: the export
    takes up to 24h and does not backfill."""
    use(monkeypatch, FakeBQ(rows=[]))

    from app.tools.preflight import _billing_check

    check = _billing_check()
    assert check["status"] == "ok"
    assert "0 attributed" in check["detail"]
    assert "24h" in check["detail"]
