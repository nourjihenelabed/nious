"""Does this data fit the profile? Answered before anything is computed on it."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pandas as pd

from ..spec import Profile

#: How many rows to sample when testing whether a column will parse.
SAMPLE_ROWS = 200


class Fit(str, Enum):
    """How well data matches a profile."""

    EXACT = "exact"
    USABLE = "usable"
    UNUSABLE = "unusable"


@dataclass(frozen=True)
class TableReport:
    """One logical table: is it there, and does it have the columns it needs?"""

    logical: str
    expected_file: str
    found: bool
    n_rows: int = 0
    missing_columns: tuple[str, ...] = ()
    untyped_columns: tuple[str, ...] = ()
    other_columns: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when this table is present and complete enough to read."""
        return self.found and not self.missing_columns and not self.error


@dataclass
class Conformance:
    """The verdict for one dataset against one profile."""

    profile_name: str
    location: str
    tables: tuple[TableReport, ...]
    engine_error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def missing_tables(self) -> tuple[str, ...]:
        """Logical tables the profile expects and the data does not have."""
        return tuple(t.logical for t in self.tables if not t.found)

    @property
    def missing_columns(self) -> dict[str, tuple[str, ...]]:
        """Per table, the columns the profile needs and the data does not have."""
        return {t.logical: t.missing_columns for t in self.tables if t.missing_columns}

    @property
    def untyped_columns(self) -> dict[str, tuple[str, ...]]:
        """Per table, columns that should be numbers or dates and would not parse."""
        return {t.logical: t.untyped_columns for t in self.tables if t.untyped_columns}

    @property
    def fit(self) -> Fit:
        """EXACT, USABLE or UNUSABLE."""
        if self.engine_error or self.missing_tables or self.missing_columns:
            return Fit.UNUSABLE
        if self.untyped_columns:
            return Fit.USABLE
        return Fit.EXACT

    @property
    def score(self) -> float:
        """Share of expected tables that are present and complete, 0-100."""
        if not self.tables:
            return 0.0
        return round(100.0 * sum(t.ok for t in self.tables) / len(self.tables), 1)

    def to_frame(self) -> pd.DataFrame:
        """The per-table verdict as a table."""
        return pd.DataFrame(
            [
                {
                    "table": t.logical,
                    "expected_file": t.expected_file,
                    "found": t.found,
                    "rows": t.n_rows,
                    "missing_columns": ", ".join(t.missing_columns),
                    "would_not_parse": ", ".join(t.untyped_columns),
                    "other_columns": ", ".join(t.other_columns),
                    "ok": t.ok,
                }
                for t in self.tables
            ]
        )

    def __str__(self) -> str:
        """The verdict as printable text, problems first."""
        head = (
            f"Conformance: {self.fit.value.upper()} "
            f"({self.score}% of tables complete) -- {self.profile_name}"
        )
        lines = [head, f"  data: {self.location}", ""]

        if self.missing_tables:
            lines.append("  Missing tables:")
            lines += [
                f"    - {t.logical:<22} expected {t.expected_file}"
                for t in self.tables
                if not t.found
            ]
            lines.append("")
        if self.missing_columns:
            lines.append("  Missing columns:")
            lines += [
                f"    - {table}: {', '.join(cols)}"
                for table, cols in self.missing_columns.items()
            ]
            lines.append("")
        if self.untyped_columns:
            lines.append("  Present but would not parse as numbers or dates:")
            lines += [
                f"    - {table}: {', '.join(cols)}"
                for table, cols in self.untyped_columns.items()
            ]
            lines.append("")
        if self.engine_error:
            lines += ["  The engine could not read it:", f"    {self.engine_error}", ""]
        if self.fit is Fit.EXACT:
            lines.append("  Every table and column this profile reads is present and typed.")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _required_columns(profile: Profile) -> dict[str, set[str]]:
    """Per logical table, the columns this profile declares a type for."""
    required: dict[str, set[str]] = {name: set() for name in profile.tables}
    for table in profile.tables:
        required[table].update(profile.numeric_columns(table))
        required[table].update(profile.date_columns(table))
    return required


def _would_not_parse(df: pd.DataFrame, columns: tuple[str, ...], kind: str) -> tuple[str, ...]:
    """Columns whose values do not survive the conversion the profile asks for."""
    bad = []
    for col in columns:
        if col not in df.columns:
            continue
        raw = df[col].head(SAMPLE_ROWS)
        populated = raw.notna() & (raw.astype(str).str.strip() != "")
        if not populated.any():
            continue
        converted = (
            pd.to_numeric(raw, errors="coerce")
            if kind == "numeric"
            else pd.to_datetime(raw, errors="coerce")
        )
        if converted[populated].isna().any():
            bad.append(col)
    return tuple(bad)


def check(source, profile: Profile | None = None) -> Conformance:
    """Check a source, a directory or a graph export against a profile."""
    from ..graph.json_source import GraphJsonSource, NotAGraphExport, is_export_folder
    from ..graph.source import CsvGraphSource, Source
    from ..profiles import get_profile

    profile = profile or get_profile()

    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            return Conformance(
                profile_name=profile.name,
                location=str(path),
                tables=(),
                engine_error=f"no such file or directory: {path}",
            )
        if path.is_file() or is_export_folder(path):
            try:
                source = GraphJsonSource(path, profile=profile)
            except NotAGraphExport as exc:
                return Conformance(
                    profile_name=profile.name,
                    location=str(path),
                    tables=(),
                    engine_error=str(exc),
                )
        else:
            source = CsvGraphSource(path, profile=profile)

    if not isinstance(source, Source):
        raise TypeError(f"expected a Source or a path, got {type(source).__name__}")

    required = _required_columns(profile)
    reports: list[TableReport] = []

    for logical in profile.tables:
        expected = (
            str(source.path_for(logical).name)
            if hasattr(source, "path_for")
            else str(profile.tables[logical])
        )
        try:
            df = source.table(logical)
            raw = source.raw_table(logical)
        except FileNotFoundError:
            reports.append(TableReport(logical, expected, found=False))
            continue
        except Exception as exc:
            reports.append(TableReport(logical, expected, found=False, error=str(exc)))
            continue

        have = set(df.columns)
        wanted = required.get(logical, set())
        reports.append(
            TableReport(
                logical=logical,
                expected_file=expected,
                found=True,
                n_rows=len(df),
                missing_columns=tuple(sorted(wanted - have)),
                untyped_columns=(
                    _would_not_parse(raw, profile.numeric_columns(logical), "numeric")
                    + _would_not_parse(raw, profile.date_columns(logical), "date")
                ),
                other_columns=tuple(sorted(have - wanted))[:12],
            )
        )

    # The declared columns are a lower bound, so run the extraction too. It is the
    # only check that knows what the profile's own code actually reaches for.
    engine_error = ""
    notes: list[str] = []
    if all(r.found for r in reports) and profile.build_facts is not None:
        try:
            extract = profile.build_facts(source)
            notes.append(
                f"extraction produced {len(extract.lines):,} fact rows "
                f"and {len(extract.keys):,} document rows"
            )
        except Exception as exc:
            engine_error = f"{type(exc).__name__}: {exc}"

    return Conformance(
        profile_name=profile.name,
        location=str(
            getattr(
                source, "data_dir",
                getattr(source, "export_path", getattr(source, "workbook_path", "?")),
            )
        ),
        tables=tuple(reports),
        engine_error=engine_error,
        notes=tuple(notes),
    )


def identify(target: Path | str) -> pd.DataFrame:
    """Which registered profile does this folder or graph export look like?"""
    from ..profiles import REGISTRY

    rows = []
    for name, profile in REGISTRY.items():
        verdict = check(target, profile)
        rows.append(
            {
                "profile": name,
                "fit": verdict.fit.value,
                "score": verdict.score,
                "tables_found": sum(t.found for t in verdict.tables),
                "tables_expected": len(verdict.tables),
                "first_problem": (
                    verdict.engine_error
                    or (
                        f"missing table {verdict.missing_tables[0]}"
                        if verdict.missing_tables
                        else ""
                    )
                    or (
                        f"missing columns in {next(iter(verdict.missing_columns))}"
                        if verdict.missing_columns
                        else ""
                    )
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("score", ascending=False, ignore_index=True)


def describe(profile: Profile | None = None) -> pd.DataFrame:
    """What data would have to look like for this profile to accept it."""
    from ..profiles import get_profile

    profile = profile or get_profile()
    rows = []
    for logical in profile.tables:
        numeric = profile.numeric_columns(logical)
        dates = profile.date_columns(logical)
        rows.append(
            {
                "table": logical,
                "file_name": profile.file_for(logical),
                "workbook_sheet": profile.tables[logical],
                "must_parse_as_number": ", ".join(numeric),
                "must_parse_as_date": ", ".join(dates),
            }
        )
    return pd.DataFrame(rows)
