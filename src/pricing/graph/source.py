"""
Graph-access layer (D1, early draft).

The rest of the engine only ever talks to a `GraphSource`. Two implementations:

  CsvGraphSource   -- ACTIVE NOW. Reads the synthetic CSV snapshot. This is the
                      placeholder while we wait for Neo4j credentials.
  Neo4jGraphSource -- WRITTEN BUT NOT YET EXERCISED. Reads the real read-only
                      graph via the official driver, using the queries in
                      cypher.py. Switch to it by setting PRICING_SOURCE=neo4j
                      and filling in NEO4J_* in .env -- no other code changes.

Both return pandas DataFrames with *identical* column names and dtypes, which
is the whole point: Week 3's waterfall engine will not know or care which one
it is reading from.

Design notes
------------
* Read-only by contract. Nothing here writes to the graph.
* Degrade gracefully. Missing optional fields come back as NaN, never as 0.
  Silently zeroing a missing discount would understate leakage -- exactly the
  failure mode the acceptance criteria call out.
* Snapshot metadata. `snapshot_info()` records where the data came from and a
  content hash, so a result can be traced back to the exact input (Brief 7.4).
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import config
from . import cypher

# Columns we coerce to a specific dtype regardless of backend, so CSV and Neo4j
# cannot drift. Anything not listed keeps its natural inferred type.
_NUMERIC = {
    "transactions": ["gross_amount", "net_amount", "quantity", "unit_price"],
    "transaction_lines": ["quantity", "unit_price", "line_total"],
    "transaction_discounts": ["discount_amount_applied"],
    "discounts": ["rate", "amount"],
    "customers": ["tenure_years"],
    "products": ["list_price", "unit_cost"],
    "channels": ["cost_to_serve", "commission_rate"],
    "regions": [],
}
_DATES = {"transactions": ["date"], "discounts": ["valid_from", "valid_to"]}


def _normalise(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Apply the semantic types from Brief section 5.3, without inventing values."""
    for col in _NUMERIC.get(name, []):
        if col in df.columns:
            # errors="coerce" turns unparseable junk into NaN rather than crashing;
            # the profiler then reports it as a data-quality finding.
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in _DATES.get(name, []):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


class GraphSource(ABC):
    """The one interface the pricing engine is allowed to depend on."""

    name: str = "abstract"

    @abstractmethod
    def table(self, name: str) -> pd.DataFrame:
        """Return one logical table by name (see config.CSV_FILES for the list)."""

    def tables(self) -> dict[str, pd.DataFrame]:
        return {n: self.table(n) for n in config.CSV_FILES}

    # -- convenience accessors, so callers read like domain language ---------
    def transactions(self) -> pd.DataFrame:
        return self.table("transactions")

    def transaction_lines(self) -> pd.DataFrame:
        return self.table("transaction_lines")

    def transaction_discounts(self) -> pd.DataFrame:
        return self.table("transaction_discounts")

    def discounts(self) -> pd.DataFrame:
        return self.table("discounts")

    def customers(self) -> pd.DataFrame:
        return self.table("customers")

    def products(self) -> pd.DataFrame:
        return self.table("products")

    def channels(self) -> pd.DataFrame:
        return self.table("channels")

    def regions(self) -> pd.DataFrame:
        return self.table("regions")

    def customer_transactions(self, customer_id: str) -> pd.DataFrame:
        """All transactions for one customer. Overridden by Neo4j to push down the filter."""
        txn = self.transactions()
        return txn[txn["customer_id"] == customer_id].copy()

    @abstractmethod
    def snapshot_info(self) -> dict:
        """Provenance of this read: backend, location, timestamp, content hash."""


# ==========================================================================
# CSV backend -- ACTIVE
# ==========================================================================
class CsvGraphSource(GraphSource):
    """
    Reads the CSV snapshot produced either by NIOUS or by our own
    scripts/export_neo4j_to_csv.py.

    Tables are cached after first read: the snapshot is immutable within a run,
    which is what makes a run reproducible.
    """

    name = "csv"

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir or config.CSV_DIR)
        if not self.data_dir.exists():
            raise FileNotFoundError(
                f"CSV data directory not found: {self.data_dir}\n"
                "Set PRICING_DATA_DIR in .env, or run scripts/export_neo4j_to_csv.py."
            )
        self._cache: dict[str, pd.DataFrame] = {}

    def table(self, name: str) -> pd.DataFrame:
        if name not in config.CSV_FILES:
            raise KeyError(f"Unknown table {name!r}. Known: {sorted(config.CSV_FILES)}")
        if name not in self._cache:
            path = self.data_dir / config.CSV_FILES[name]
            if not path.exists():
                raise FileNotFoundError(f"Missing snapshot file: {path}")
            # keep_default_na=True so empty cells become NaN, NOT 0 or "".
            self._cache[name] = _normalise(name, pd.read_csv(path))
        return self._cache[name].copy()

    def snapshot_info(self) -> dict:
        h = hashlib.sha256()
        for fname in sorted(config.CSV_FILES.values()):
            p = self.data_dir / fname
            if p.exists():
                h.update(p.read_bytes())
        return {
            "backend": "csv",
            "location": str(self.data_dir),
            "read_at": datetime.now(timezone.utc).isoformat(),
            "content_sha256": h.hexdigest(),
        }


# ==========================================================================
# Neo4j backend -- READY, NOT YET RUN
# ==========================================================================
class Neo4jGraphSource(GraphSource):
    """
    Reads the live read-only AXIOM graph.

    Not exercised yet -- we have no credentials as of Week 1. The code is here
    so that the day the URI arrives, the only change is in .env:

        PRICING_SOURCE=neo4j
        NEO4J_URI=bolt://<host>:7687
        NEO4J_USER=...
        NEO4J_PASSWORD=...

    Requires:  pip install neo4j

    Safety: the driver session is opened in READ access mode, so even a coding
    mistake cannot write to the client graph.
    """

    name = "neo4j"

    def __init__(self, settings: config.Neo4jSettings | None = None):
        try:
            from neo4j import GraphDatabase  # imported lazily: optional dependency
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The Neo4j backend needs the driver: pip install neo4j\n"
                "(Week 1 runs on PRICING_SOURCE=csv and does not need it.)"
            ) from exc

        self.settings = settings or config.NEO4J
        if not self.settings.is_configured():
            raise RuntimeError(
                "NEO4J_PASSWORD is empty -- fill in .env before using the Neo4j backend."
            )
        self._driver = GraphDatabase.driver(
            self.settings.uri, auth=(self.settings.user, self.settings.password)
        )
        self._cache: dict[str, pd.DataFrame] = {}

    # -- low-level -----------------------------------------------------------
    def run(self, query: str, **params) -> pd.DataFrame:
        """Execute a read-only Cypher query and return a DataFrame."""
        from neo4j import READ_ACCESS

        with self._driver.session(
            database=self.settings.database, default_access_mode=READ_ACCESS
        ) as session:
            result = session.run(query, **params)
            return pd.DataFrame([dict(r) for r in result])

    # -- GraphSource ---------------------------------------------------------
    def table(self, name: str) -> pd.DataFrame:
        if name not in cypher.QUERIES:
            raise KeyError(f"Unknown table {name!r}")
        if name not in self._cache:
            self._cache[name] = _normalise(name, self.run(cypher.QUERIES[name]))
        return self._cache[name].copy()

    def customer_transactions(self, customer_id: str) -> pd.DataFrame:
        # Push the filter into Cypher rather than pulling the whole graph.
        return _normalise(
            "transactions",
            self.run(cypher.CUSTOMER_TRANSACTIONS, customer_id=customer_id),
        )

    def snapshot_info(self) -> dict:
        counts = self.run(cypher.NODE_COUNTS)
        return {
            "backend": "neo4j",
            "location": self.settings.uri,
            "database": self.settings.database,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "node_counts": json.loads(counts.to_json(orient="records")),
        }

    def close(self) -> None:
        self._driver.close()


# ==========================================================================
# Factory
# ==========================================================================
def get_source(source: str | None = None, **kwargs) -> GraphSource:
    """
    Return the configured backend.

    >>> src = get_source()          # honours PRICING_SOURCE, defaults to csv
    >>> src.transactions().shape
    """
    source = (source or config.SOURCE).lower()
    if source == "csv":
        return CsvGraphSource(**kwargs)
    if source == "neo4j":
        return Neo4jGraphSource(**kwargs)
    raise ValueError(f"Unknown PRICING_SOURCE={source!r}. Use 'csv' or 'neo4j'.")
