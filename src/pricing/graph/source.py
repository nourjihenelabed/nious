"""Graph-access layer (D1): one Source interface, four backends behind it."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import config
from . import cypher


def _normalise(name: str, df: pd.DataFrame, profile=None) -> pd.DataFrame:
    """Apply the profile's semantic types, without inventing values."""
    if profile is None:
        from ..profiles import get_profile

        profile = get_profile()
    for col in profile.numeric_columns(name):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in profile.date_columns(name):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


class Source(ABC):
    """The one interface the pricing engine is allowed to depend on."""

    name: str = "abstract"

    @abstractmethod
    def table(self, name: str) -> pd.DataFrame:
        """Return one logical table by name."""

    def table_names(self) -> tuple[str, ...]:
        """The logical tables this source can serve."""
        return tuple(config.CSV_FILES)

    def tables(self) -> dict[str, pd.DataFrame]:
        """Every logical table, keyed by name."""
        return {n: self.table(n) for n in self.table_names()}

    def raw_table(self, name: str) -> pd.DataFrame:
        """One logical table *before* semantic types are applied."""
        return self.table(name)

    @abstractmethod
    def snapshot_info(self) -> dict:
        """Provenance of this read: backend, location, timestamp, content hash."""


class GraphSource(Source):
    """A `Source` over the AXIOM graph's own entity names."""

    def transactions(self) -> pd.DataFrame:
        """Transaction headers."""
        return self.table("transactions")

    def transaction_lines(self) -> pd.DataFrame:
        """CONTAINS edges: one row per transaction line."""
        return self.table("transaction_lines")

    def transaction_discounts(self) -> pd.DataFrame:
        """DISCOUNTED_BY edges, with the amount applied."""
        return self.table("transaction_discounts")

    def discounts(self) -> pd.DataFrame:
        """Discount nodes, including the on/off-invoice type."""
        return self.table("discounts")

    def customers(self) -> pd.DataFrame:
        """Customer nodes, with segment and region."""
        return self.table("customers")

    def products(self) -> pd.DataFrame:
        """Product nodes, with list price and unit cost."""
        return self.table("products")

    def channels(self) -> pd.DataFrame:
        """Channel nodes."""
        return self.table("channels")

    def regions(self) -> pd.DataFrame:
        """Region nodes, including the currency."""
        return self.table("regions")

    def customer_transactions(self, customer_id: str) -> pd.DataFrame:
        """All transactions for one customer."""
        txn = self.transactions()
        return txn[txn["customer_id"] == customer_id].copy()


class CsvGraphSource(GraphSource):
    """Reads a directory of CSVs, caching each table for the life of the run."""

    name = "csv"

    def __init__(self, data_dir: Path | str | None = None, profile=None):
        """Point at a snapshot directory, failing loudly if it is not there."""
        from ..profiles import get_profile

        self.profile = profile or get_profile()
        self.data_dir = Path(data_dir or config.CSV_DIR)
        if not self.data_dir.exists():
            raise FileNotFoundError(
                f"CSV data directory not found: {self.data_dir}\n"
                "Set PRICING_DATA_DIR in .env, or run scripts/export_neo4j_to_csv.py."
            )
        self._cache: dict[str, pd.DataFrame] = {}

    def table_names(self) -> tuple[str, ...]:
        """The logical tables this profile expects to find here."""
        return tuple(self.profile.tables)

    def path_for(self, name: str) -> Path:
        """Where one logical table is expected on disk."""
        return self.data_dir / self.profile.file_for(name)

    def raw_table(self, name: str) -> pd.DataFrame:
        """The file exactly as written, every column as text."""
        path = self.path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"Missing snapshot file: {path}")
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def table(self, name: str) -> pd.DataFrame:
        """One logical table, read once and normalised."""
        if name not in self.profile.tables:
            raise KeyError(
                f"Unknown table {name!r} for profile {self.profile.name!r}. "
                f"Known: {sorted(self.profile.tables)}"
            )
        if name not in self._cache:
            path = self.path_for(name)
            if not path.exists():
                raise FileNotFoundError(
                    f"Missing snapshot file: {path}\n"
                    f"Profile {self.profile.name!r} expects it for the {name!r} table."
                )
            self._cache[name] = _normalise(name, pd.read_csv(path), self.profile)
        return self._cache[name].copy()

    def snapshot_info(self) -> dict:
        """Backend, location, read time, and a sha256 over the snapshot bytes."""
        h = hashlib.sha256()
        for name in sorted(self.profile.tables):
            p = self.path_for(name)
            if p.exists():
                h.update(p.read_bytes())
        return {
            "backend": "csv",
            "location": str(self.data_dir),
            "profile": self.profile.name,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "content_sha256": h.hexdigest(),
        }


class Neo4jGraphSource(GraphSource):
    """Reads the live AXIOM graph over Bolt in READ access mode. Not yet exercised."""

    name = "neo4j"

    def __init__(self, settings: config.Neo4jSettings | None = None):
        """Open a driver, refusing to start without credentials."""
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The Neo4j backend needs the driver: pip install neo4j\n"
                "(The CSV backend does not need it.)"
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

    def run(self, query: str, **params) -> pd.DataFrame:
        """Execute a read-only Cypher query and return a DataFrame."""
        from neo4j import READ_ACCESS

        with self._driver.session(
            database=self.settings.database, default_access_mode=READ_ACCESS
        ) as session:
            result = session.run(query, **params)
            return pd.DataFrame([dict(r) for r in result])

    def table(self, name: str) -> pd.DataFrame:
        """One logical table, from its query in cypher.py."""
        if name not in cypher.QUERIES:
            raise KeyError(f"Unknown table {name!r}")
        if name not in self._cache:
            self._cache[name] = _normalise(name, self.run(cypher.QUERIES[name]))
        return self._cache[name].copy()

    def table_names(self) -> tuple[str, ...]:
        """The logical tables the Cypher layer can serve."""
        return tuple(cypher.QUERIES)

    def customer_transactions(self, customer_id: str) -> pd.DataFrame:
        """All transactions for one customer, filtered inside Cypher."""
        return _normalise(
            "transactions",
            self.run(cypher.CUSTOMER_TRANSACTIONS, customer_id=customer_id),
        )

    def snapshot_info(self) -> dict:
        """Backend, URI, read time, and node counts as a coarse fingerprint."""
        counts = self.run(cypher.NODE_COUNTS)
        return {
            "backend": "neo4j",
            "location": self.settings.uri,
            "database": self.settings.database,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "node_counts": json.loads(counts.to_json(orient="records")),
        }

    def close(self) -> None:
        """Release the driver's connection pool."""
        self._driver.close()


def get_source(source: str | None = None, **kwargs) -> Source:
    """Return the backend named by PRICING_SOURCE, or the one asked for."""
    if source is None and not config.SOURCE_IS_EXPLICIT:
        from ..profiles import get_profile

        profile = kwargs.get("profile") or get_profile()
        source = getattr(profile, "default_source", None)

    source = (source or config.SOURCE).lower()
    if source == "csv":
        return CsvGraphSource(**kwargs)
    if source in ("graph_json", "json"):
        from .json_source import GraphJsonSource

        return GraphJsonSource(**kwargs)
    if source == "neo4j":
        return Neo4jGraphSource(**kwargs)
    if source == "workbook":
        from ..mapping.workbook import WorkbookSource
        from ..profiles import get_profile

        kwargs.setdefault("workbook", config.WORKBOOK)
        kwargs.setdefault("mapping", config.SOURCE_MAPPING)
        kwargs.setdefault("tables", get_profile().tables)
        return WorkbookSource(**kwargs)
    raise ValueError(
        f"Unknown PRICING_SOURCE={source!r}. "
        "Use 'graph_json', 'csv', 'neo4j' or 'workbook'."
    )
