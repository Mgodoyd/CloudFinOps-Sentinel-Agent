#!/usr/bin/env python3
"""Find the Cloud Billing export table and write it into .env.

Enabling the export creates nothing immediately: Google starts writing rows
within about 24 hours, and only from that moment forward — there is no
backfill. So the dataset exists and stays empty for a day, which looks
identical to a misconfiguration. This says which of the two it is.

Run it until it finds the table:

    python scripts/billing_table.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402

# The agent's query reads resource.name, which only the detailed export has.
# Standard export tables would be accepted here and then fail at query time.
DETAILED = re.compile(r"^gcp_billing_export_resource_v1_")
STANDARD = re.compile(r"^gcp_billing_export_v1_")


def main() -> int:
    from google.cloud import bigquery

    client = bigquery.Client(project=settings.PROJECT_ID)
    detailed, standard = [], []

    for dataset in client.list_datasets():
        for table in client.list_tables(dataset.dataset_id):
            full = f"{settings.PROJECT_ID}.{dataset.dataset_id}.{table.table_id}"
            if DETAILED.match(table.table_id):
                detailed.append(full)
            elif STANDARD.match(table.table_id):
                standard.append(full)

    if not detailed:
        if standard:
            print("Found a STANDARD usage cost export:")
            for t in standard:
                print("  ", t)
            print("\nThe agent needs the DETAILED export — it reads resource.name,")
            print("which the standard export does not have. Enable 'Detailed usage")
            print("cost' under Billing → Billing export → BigQuery export.")
            return 1
        print("No billing export table yet.")
        print("Google starts writing rows up to 24h after you enable the export,")
        print("and does not backfill. An empty dataset today is expected.")
        return 1

    table = detailed[0]
    print(f"Found: {table}")

    env = open(".env").read() if os.path.exists(".env") else ""
    line = f"BILLING_EXPORT_TABLE={table}"
    if re.search(r"^BILLING_EXPORT_TABLE=.*$", env, re.M):
        env = re.sub(r"^BILLING_EXPORT_TABLE=.*$", line, env, flags=re.M)
    else:
        env = env.rstrip("\n") + f"\n{line}\n"
    open(".env", "w").write(env)

    print("Written to .env.")

    # A dry run parses the SQL against the real table for free. The unit tests
    # all mock BigQuery, so this is the only place the query is actually checked.
    os.environ["BILLING_EXPORT_TABLE"] = table
    settings.BILLING_EXPORT_TABLE = table
    from app.tools import gcp_billing

    problem = gcp_billing.validate_query()
    if problem:
        print(f"\nThe query does not run against this table:\n  {problem}")
        return 1
    print("Query validated against the table (dry run, no cost).")

    print("\nRestart the service — configuration is read once at startup —")
    print("then check /api/preflight for 'Cost source'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
