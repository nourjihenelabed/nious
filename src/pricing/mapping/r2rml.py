"""Read an AXIOM source mapping: which spreadsheet column becomes which graph node."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def template_columns(template: str) -> tuple[str, ...]:
    """The column names a subject template implies: `{year}-{month}` -> Year, Month."""
    return tuple(
        "_".join(
            part.capitalize() if part.islower() else part
            for part in name.strip().split("_")
        )
        for name in _PLACEHOLDER.findall(template or "")
    )


#: Discharge codes the mapping uses for a relationship whose key is absent from
#: the sheet. Anything carrying one of these cannot be joined from this sheet.
UNJOINABLE = {"none-on-this-sheet"}


@dataclass(frozen=True)
class Relation:
    """One edge declared by a triplesMap: predicate, target class, join key."""

    predicate: str
    target_class: str
    join_columns: tuple[str, ...] = ()
    discharge: str = ""

    @property
    def joinable(self) -> bool:
        """True when this sheet actually carries the columns to make the edge."""
        return bool(self.join_columns) and self.discharge not in UNJOINABLE


@dataclass(frozen=True)
class ClassMap:
    """One triplesMap: a class materialised from one sheet."""

    sheet: str
    class_name: str
    identity_columns: tuple[str, ...] = ()
    property_columns: tuple[str, ...] = ()
    relations: tuple[Relation, ...] = ()
    template: str = ""

    def columns(self) -> tuple[str, ...]:
        """Every column of the sheet this class map touches, in declaration order."""
        seen: dict[str, None] = {}
        for col in (*self.identity_columns, *self.property_columns):
            seen.setdefault(col, None)
        for rel in self.relations:
            for col in rel.join_columns:
                seen.setdefault(col, None)
        return tuple(seen)


@dataclass(frozen=True)
class SourceMapping:
    """A whole mapping file: every class map, indexed by sheet and by class."""

    workbook: str
    class_maps: tuple[ClassMap, ...]
    artifact_version: str = ""
    path: Path | None = None
    format: str = ""

    _by_sheet: dict[str, list[ClassMap]] = field(default_factory=dict, repr=False, compare=False)
    _by_class: dict[str, list[ClassMap]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Build the sheet and class indexes once, at construction."""
        for cm in self.class_maps:
            self._by_sheet.setdefault(cm.sheet, []).append(cm)
            self._by_class.setdefault(cm.class_name, []).append(cm)

    # -- lookups -----------------------------------------------------------

    @property
    def sheets(self) -> tuple[str, ...]:
        """Sheet names in mapping order."""
        return tuple(self._by_sheet)

    @property
    def classes(self) -> tuple[str, ...]:
        """Class names in mapping order."""
        return tuple(self._by_class)

    def for_sheet(self, sheet: str) -> tuple[ClassMap, ...]:
        """Every class materialised from one sheet."""
        return tuple(self._by_sheet.get(sheet, ()))

    def for_class(self, class_name: str) -> tuple[ClassMap, ...]:
        """Every sheet that materialises one class."""
        return tuple(self._by_class.get(class_name, ()))

    def sheet_columns(self, sheet: str, infer: bool = True) -> tuple[str, ...]:
        """The column set the mapping implies for a sheet, in declaration order."""
        seen: dict[str, None] = {}
        for cm in self.for_sheet(sheet):
            for col in cm.columns():
                seen.setdefault(col, None)
        if seen or not infer:
            return tuple(seen)
        for cm in self.for_sheet(sheet):
            for col in template_columns(cm.template):
                seen.setdefault(col, None)
        return tuple(seen)

    def owner_of(self, sheet: str, class_name: str) -> ClassMap | None:
        """The class map for one class on one sheet, if it is declared there."""
        for cm in self.for_sheet(sheet):
            if cm.class_name == class_name:
                return cm
        return None

    def primary_sheet_for(self, class_name: str) -> str | None:
        """The sheet that *defines* a class: the one carrying the most of it."""
        maps = self.for_class(class_name)
        if not maps:
            return None
        return max(maps, key=lambda cm: (len(cm.columns()), -self.class_maps.index(cm))).sheet

    # -- construction ------------------------------------------------------

    @staticmethod
    def from_dict(payload: dict, path: Path | None = None) -> SourceMapping:
        """Parse a decoded mapping document."""
        maps: list[ClassMap] = []
        workbook = ""
        for tm in payload.get("triplesMaps", []):
            table = str(tm.get("logicalTable", {}).get("tableName", ""))
            book, _, sheet = table.partition("::")
            workbook = workbook or book

            subject = tm.get("subjectMap", {})
            props: list[str] = []
            rels: list[Relation] = []
            for pom in tm.get("predicateObjectMaps", []):
                om = pom.get("objectMap", {})
                if "column" in om:
                    props.append(str(om["column"]))
                elif "parentTriplesMap" in om:
                    rels.append(
                        Relation(
                            predicate=str(pom.get("predicate", "")),
                            target_class=str(om["parentTriplesMap"]),
                            join_columns=tuple(str(c) for c in om.get("joinColumns", ())),
                            discharge=str(om.get("discharge", "")),
                        )
                    )

            maps.append(
                ClassMap(
                    sheet=sheet or table,
                    class_name=str(subject.get("class", "")),
                    identity_columns=tuple(str(c) for c in subject.get("identityColumns", ())),
                    property_columns=tuple(props),
                    relations=tuple(rels),
                    template=str(subject.get("template") or ""),
                )
            )

        return SourceMapping(
            workbook=workbook,
            class_maps=tuple(maps),
            artifact_version=str(payload.get("artifact_version", "")),
            path=path,
            format=str(payload.get("format", "")),
        )

    @staticmethod
    def load(path: Path | str) -> SourceMapping:
        """Read and parse a mapping file from disk."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Source mapping not found: {p}")
        return SourceMapping.from_dict(json.loads(p.read_text(encoding="utf-8")), path=p)


def find_mapping(start: Path | str | None = None) -> Path | None:
    """Locate an `axiom-source-mapping*.json` at or above `start`."""
    here = Path(start or Path(__file__).resolve()).resolve()
    for directory in (here, *here.parents):
        if not directory.is_dir():
            continue
        hits = sorted(directory.glob("axiom-source-mapping*.json"))
        if hits:
            return hits[0]
    return None
