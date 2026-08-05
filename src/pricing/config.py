"""
Central configuration for the AXIOM Pricing Module.

Everything that could differ between "laptop with CSVs" and "NIOUS dev box with
Neo4j" lives here, so no other module needs to know which backend is active.

Read from environment variables (see .env.example). Nothing here reaches the
network at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# repo root = .../axiom-pricing-module
REPO_ROOT = Path(__file__).resolve().parents[2]

# --- where snapshots live -------------------------------------------------
# Canonical location: data/ inside the repo, so DVC can version it (DVC cannot
# track paths outside the project) and so `export_neo4j_to_csv.py` has an
# obvious place to write. Each snapshot is its own dated/named directory:
#
#     data/snapshot_synthetic_v1/     <- the Week 1 placeholder
#     data/snapshot_2026-07-08/       <- a real export, once we have credentials
#
# The newest `snapshot_*` directory wins unless PRICING_DATA_DIR says otherwise.
DATA_DIR = REPO_ROOT / "data"


def _default_snapshot() -> Path:
    candidates = sorted(
        (p for p in DATA_DIR.glob("snapshot_*") if p.is_dir()),
        key=lambda p: p.name,
    )
    if candidates:
        # dated snapshots sort after "snapshot_synthetic_v1", so a real export
        # automatically takes precedence over the placeholder
        return candidates[-1]
    # fall back to the original hand-off location next to the repo
    return REPO_ROOT.parent / "synthetic"


DEFAULT_CSV_DIR = _default_snapshot()

REPORTS_DIR = REPO_ROOT / "reports"
DOCS_DIR = REPO_ROOT / "docs"


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------
# PRICING_SOURCE = "csv"   -> read the CSV snapshot  (current, Week 1)
# PRICING_SOURCE = "neo4j" -> read the live graph    (once NIOUS gives us creds)
SOURCE = os.getenv("PRICING_SOURCE", "csv").strip().lower()

CSV_DIR = Path(os.getenv("PRICING_DATA_DIR", str(DEFAULT_CSV_DIR)))


@dataclass(frozen=True)
class Neo4jSettings:
    """Connection settings for the read-only AXIOM knowledge graph."""

    uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.getenv("NEO4J_USER", "neo4j")
    password: str = os.getenv("NEO4J_PASSWORD", "")
    database: str = os.getenv("NEO4J_DATABASE", "neo4j")

    def is_configured(self) -> bool:
        return bool(self.password)


NEO4J = Neo4jSettings()


# --------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------
# Discount taxonomy from the Data Contract (Brief section 5.1).
ON_INVOICE = "on-invoice"
OFF_INVOICE = "off-invoice"

# The canonical CSV filenames. The Neo4j -> CSV exporter writes exactly these
# names, so swapping backends never changes the rest of the code.
CSV_FILES = {
    "transactions": "transactions.csv",
    "transaction_lines": "transaction_lines.csv",
    "transaction_discounts": "transaction_discounts.csv",
    "discounts": "discounts.csv",
    "customers": "customers.csv",
    "products": "products.csv",
    "channels": "channels.csv",
    "regions": "regions.csv",
}
