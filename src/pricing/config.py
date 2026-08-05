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

# Default CSV location: the synthetic export that sits next to this repo.
# Override with PRICING_DATA_DIR when the real Neo4j export lands elsewhere.
DEFAULT_CSV_DIR = REPO_ROOT.parent / "synthetic"

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
