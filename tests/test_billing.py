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
    assert gcp_billing.reconcile(RESOURCE, {}) is None


def test_an_unattributed_resource_shows_nothing_rather_than_zero(monkeypatch, configured):
    """Not every charge carries a label the export can map to a service.
    Reporting $0.00 there would claim the resource is free."""
    assert gcp_billing.reconcile(RESOURCE, {"other-service": 12.0}) is None


# --- 2. reading the export -------------------------------------------------
def test_credits_are_netted_off(monkeypatch, configured):
    """Credits are negative amounts in the export; ignoring them overstates
    the bill, which for a savings agent is the flattering direction."""
    use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="checkout-api",
                                          cost=200.0, credits=-50.0)]))
    assert gcp_billing.fetch_billed_costs() == {"checkout-api": 150.0}


def test_a_missing_credit_column_is_not_a_crash(monkeypatch, configured):
    use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="a", cost=10.0, credits=None)]))
    assert gcp_billing.fetch_billed_costs() == {"a": 10.0}


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
    result = gcp_billing.reconcile(RESOURCE, {"checkout-api": 198.11})

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
    assert gcp_billing.reconcile(RESOURCE, {"checkout-api": 420.0})["delta"] == 115.16


# --- 4. it never runs where it should not ----------------------------------
def test_mock_mode_never_queries_billing(monkeypatch):
    """A simulated fleet has no invoice, and a demo must not spend money."""
    monkeypatch.setattr(settings, "BILLING_EXPORT_TABLE", "p.ds.t")
    monkeypatch.setattr(settings, "MOCK_MODE", True)
    fake = use(monkeypatch, FakeBQ(rows=[FakeRow(resource_id="a", cost=1.0, credits=0)]))

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
