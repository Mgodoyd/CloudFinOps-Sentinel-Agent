"""What Google actually charged, as opposed to what the cost model estimates.

The rest of this agent prices the *allocation*: CPU and memory at Cloud Run
Tier-1 on-demand rates, assuming an always-on instance. That is the right signal
for right-sizing — it answers "what is this shape costing me" without waiting a
month to find out — but it is not an invoice, and for a FinOps agent that gap is
the most obvious thing to challenge.

The billing export closes it. Enabling it writes one row per SKU per day into
BigQuery, and that is the number finance sees. Reading it lets the agent say two
different things at once:

    estimated $304.84/mo   ← what the allocation should cost
    billed    $198.11/mo   ← what it actually cost last month

A gap in either direction is information rather than an error. Billed *under*
estimate usually means a scale-to-zero service really is idle most of the time,
so the estimate was a ceiling. Billed *over* usually means egress, CPU boost or
requests the allocation model does not price at all.

The export is optional and off by default: it takes a day to start producing
rows, costs money to query, and a demo has to run without it. Everything here
degrades to None, and the estimate stands alone exactly as it did before.
"""

import logging
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Why the last read failed, kept so the readiness report can say something
# actionable. Swallowing the exception and reporting "could not read" sends the
# operator to check permissions that are already correct.
_last_error: Optional[Exception] = None


def last_error() -> Optional[Exception]:
    return _last_error

# One query per audit, over the last full month, grouped by service. The billing
# export is partitioned on usage date and charged by bytes scanned, so the date
# filter is what keeps this cheap rather than a nicety.
_QUERY = """
SELECT
  MIN(usage_start_time) OVER () AS window_start,
  MAX(usage_start_time) OVER () AS window_end,
  COALESCE(
    (SELECT value FROM UNNEST(labels) WHERE key = 'goog-cloud-run-service-name'),
    (SELECT value FROM UNNEST(labels) WHERE key = 'goog-k8s-cluster-name'),
    resource.name,
    service.description
  ) AS resource_id,
  SUM(cost) AS cost,
  SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits
FROM `{table}`
WHERE project.id = @project
  AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY resource_id, window_start, window_end
HAVING resource_id IS NOT NULL
"""


def is_configured() -> bool:
    return bool(settings.BILLING_EXPORT_TABLE)


def _client():
    from google.cloud import bigquery

    return bigquery.Client(project=settings.PROJECT_ID)


def fetch_billed_costs() -> Optional[Dict[str, Any]]:
    """`{"costs": {resource_id: net cost}, "days_covered": n}`, or None.

    None and an empty result mean different things and both are real answers:
    None is "we could not look", empty is "we looked and Google charged nothing
    we can attribute". Only the first should make the UI stop claiming billed
    figures.

    `days_covered` is what the rows actually span, not the window that was
    asked for. The export does not backfill, so a freshly enabled one holds a
    day or two — and scaling that to a month as if it were thirty days would
    report a tenth of the real bill next to the estimate, which reads as the
    estimate being wildly wrong rather than the data being young.
    """
    global _last_error
    _last_error = None

    if not is_configured():
        return None

    from google.cloud import bigquery

    query = _QUERY.format(table=settings.BILLING_EXPORT_TABLE)
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("project", "STRING", settings.PROJECT_ID),
            bigquery.ScalarQueryParameter("days", "INT64", settings.BILLING_LOOKBACK_DAYS),
        ],
        # A runaway scan on a billing export is itself a cost incident.
        maximum_bytes_billed=settings.BILLING_MAX_BYTES,
    )

    try:
        rows = _client().query(query, job_config=config).result(
            timeout=settings.BILLING_TIMEOUT
        )
    except Exception as exc:
        _last_error = exc
        logger.warning(
            "Billing export unavailable (%s: %s); costs stay estimated",
            type(exc).__name__, str(exc)[:200],
        )
        return None

    # Credits are negative amounts in the export, so adding them is the net.
    billed: Dict[str, float] = {}
    start = end = None
    for row in rows:
        if row["resource_id"]:
            billed[row["resource_id"]] = round(
                float(row["cost"] or 0.0) + float(row["credits"] or 0.0), 2
            )
        start = start or row["window_start"]
        end = end or row["window_end"]

    days = 0.0
    if start and end:
        days = max((end - start).total_seconds() / 86400.0, 0.0)

    logger.info(
        "Billing export returned %d attributed resource(s) over %.1f day(s)",
        len(billed), days,
    )
    return {"costs": billed, "days_covered": days}


def to_monthly(cost: float, days_covered: Optional[float] = None) -> float:
    """Scale the observed window to a month, so it compares to the estimate.

    Scaled by the days the data actually spans. Using the configured window
    instead would divide a young export's charges across days it never saw.
    """
    days = days_covered if days_covered else settings.BILLING_LOOKBACK_DAYS
    return round(cost * (30.0 / max(days, 1.0)), 2)


# Below this, the export is too young for a monthly projection to mean much.
MIN_TRUSTWORTHY_DAYS = 3.0


def reconcile(
    resource: Dict[str, Any], billed: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Set the estimate against the invoice for one resource.

    Returns None when there is nothing to say — no export configured, or this
    resource has no attributable charge. Silence is correct there: showing
    "billed $0.00" for a resource the export simply does not label would be a
    worse claim than showing nothing at all.
    """
    if not billed:
        return None

    costs = billed.get("costs") or {}
    raw = costs.get(resource["resource_id"])
    if raw is None:
        return None

    days = float(billed.get("days_covered") or 0.0)
    monthly = to_monthly(raw, days)
    estimated = float(resource.get("monthly_cost") or 0.0)
    return {
        "billed_monthly": monthly,
        "estimated_monthly": round(estimated, 2),
        "delta": round(monthly - estimated, 2),
        "window_days": round(days, 1) or settings.BILLING_LOOKBACK_DAYS,
        # A day-old export projected to a month is arithmetic, not evidence.
        "partial": days < MIN_TRUSTWORTHY_DAYS,
        "source": "cloud_billing_export",
    }
