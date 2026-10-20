"""Read a mapped workbook as logical tables, and say honestly what it can support."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..graph.source import Source
from .r2rml import SourceMapping


class WorkbookSource(Source):
    """A workbook read through its source mapping, one sheet per logical table."""

    name = "workbook"

    def __init__(
        self,
        workbook: Path | str,
        mapping: SourceMapping | Path | str,
        tables: dict[str, str] | None = None,
        profile=None,
    ):
        """Open a workbook and bind it to a mapping and a logical-name lookup."""
        self.workbook_path = Path(workbook)
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Workbook not found: {self.workbook_path}")

        self.mapping = (
            mapping if isinstance(mapping, SourceMapping) else SourceMapping.load(mapping)
        )
        self.profile = profile
        self._tables = dict(tables or (profile.tables if profile else {}))
        self._cache: dict[str, pd.DataFrame] = {}

    # -- sheet access ------------------------------------------------------

    def sheet(self, sheet_name: str) -> pd.DataFrame:
        """One raw sheet, by the name it has in the workbook."""
        if sheet_name not in self._cache:
            try:
                self._cache[sheet_name] = pd.read_excel(
                    self.workbook_path, sheet_name=sheet_name, engine="openpyxl"
                )
            except ValueError as exc:
                raise KeyError(
                    f"Sheet {sheet_name!r} is not in {self.workbook_path.name}. "
                    f"The mapping expects: {', '.join(self.mapping.sheets)}"
                ) from exc
        return self._cache[sheet_name].copy()

    def table(self, name: str) -> pd.DataFrame:
        """One logical table, resolved through the profile's sheet lookup."""
        if name not in self._tables:
            raise KeyError(f"Unknown table {name!r}. Known: {sorted(self._tables)}")
        df = self.sheet(self._tables[name])
        if self.profile is not None:
            from ..graph.source import _normalise

            df = _normalise(name, df, self.profile)
        return df

    def raw_table(self, name: str) -> pd.DataFrame:
        """The sheet exactly as stored, before semantic types are applied."""
        if name not in self._tables:
            raise KeyError(f"Unknown table {name!r}. Known: {sorted(self._tables)}")
        return self.sheet(self._tables[name])

    def table_names(self) -> tuple[str, ...]:
        """The logical tables this source can serve."""
        return tuple(self._tables)

    # -- what the mapping does and does not reach --------------------------

    def sheets_present(self) -> tuple[str, ...]:
        """Sheets the workbook actually has."""
        return tuple(pd.ExcelFile(self.workbook_path, engine="openpyxl").sheet_names)

    def missing_sheets(self) -> tuple[str, ...]:
        """Sheets the mapping declares that the workbook does not have."""
        present = set(self.sheets_present())
        return tuple(s for s in self.mapping.sheets if s not in present)

    def missing_columns(self) -> dict[str, tuple[str, ...]]:
        """Per sheet, the mapped columns the workbook does not actually carry."""
        gaps: dict[str, tuple[str, ...]] = {}
        present = set(self.sheets_present())
        for sheet in self.mapping.sheets:
            if sheet not in present:
                continue
            have = set(self.sheet(sheet).columns)
            want = self.mapping.sheet_columns(sheet)
            absent = tuple(c for c in want if c not in have)
            if absent:
                gaps[sheet] = absent
        return gaps

    def snapshot_info(self) -> dict:
        """Provenance: the workbook, the mapping, and a hash over both."""
        h = hashlib.sha256()
        h.update(self.workbook_path.read_bytes())
        if self.mapping.path:
            h.update(Path(self.mapping.path).read_bytes())
        return {
            "backend": "workbook",
            "location": str(self.workbook_path),
            "mapping": str(self.mapping.path or "<in-memory>"),
            "mapping_version": self.mapping.artifact_version,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "content_sha256": h.hexdigest(),
        }
