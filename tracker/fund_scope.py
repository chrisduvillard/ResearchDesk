"""Active fund coverage. Retired fund records remain available as archives."""

TRACKED_FUNDS = ("DBMF", "KMLM", "CTA", "WTMF")
# Static application constants only; never interpolate request values into SQL.
FUND_IDS_SQL = ",".join("'" + fund + "'" for fund in TRACKED_FUNDS)
SOURCE_SCOPE_SQL = f"(fund_id IS NULL OR fund_id IN ({FUND_IDS_SQL}))"
ACTIVITY_SCOPE_SQL = (
    "(source_id IS NULL OR source_id NOT IN "
    f"(SELECT id FROM sources WHERE fund_id IS NOT NULL AND fund_id NOT IN ({FUND_IDS_SQL})))"
)
