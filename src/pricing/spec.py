"""The pricing profile: what a waterfall *is*, declared rather than hardcoded."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

    from .graph.source import Source


class Kind(str, Enum):
    """A step is a level you can stand on, or a deduction between two levels."""

    LEVEL = "level"
    DEDUCTION = "deduction"


class Origin(str, Enum):
    """Where a step's number comes from."""

    MEASURE = "measure"
    DERIVED = "derived"


#: The two grains the engine distinguishes.
LINE = "line"
KEY = "key"


@dataclass(frozen=True)
class Step:
    """One rung of the waterfall."""

    name: str
    kind: Kind
    origin: Origin
    grain: str = LINE
    label: str = ""
    formula: str = ""

    def display(self) -> str:
        """Human label, defaulting to the step name in title case."""
        return self.label or self.name.replace("_", " ").capitalize()


@dataclass(frozen=True)
class Dimension:
    """One way to slice the waterfall, and the grain at which it is constant."""

    name: str
    column: str
    grain: str = LINE
    label_column: str | None = None
    description: str = ""

    @property
    def key_safe(self) -> bool:
        """True when this dimension does not vary inside one document."""
        return self.grain == KEY


@dataclass(frozen=True)
class Profile:
    """Everything schema-specific about one client's pricing data."""

    name: str
    entity: str
    key: str
    steps: tuple[Step, ...]
    dimensions: tuple[Dimension, ...]
    tables: dict[str, str] = field(default_factory=dict)
    #: Per logical table, the columns that must parse as numbers or as dates.
    semantic_types: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    build_facts: Callable[[Source], object] | None = None
    uncertified_reasons: Callable[..., pd.Series] | None = None
    quality_flags: Callable[..., pd.DataFrame] | None = None
    validate_source: Callable[..., object] | None = None
    #: One row per decided offer -- won or lost -- with the price position and the deal facts
    #: known when the quote went out.
    win_data: Callable[[Source], object] | None = None
    #: Per product: today's list price and cost, and the reference price the win model's price
    #: position is measured from.
    reference_prices: Callable[[Source], object] | None = None
    absent_steps: dict[str, str] = field(default_factory=dict)
    currency_column: str | None = None
    date_column: str | None = None
    quantity_column: str | None = None
    #: A price the source *recorded*, as opposed to one derived as value divided by quantity.
    unit_price_column: str | None = None
    #: The backend this client's data natively lives in, when it is not a folder of CSVs.
    default_source: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        """Reject a chain that cannot be computed, at construction time."""
        problems = validate_chain(self.steps)
        if problems:
            raise ValueError(f"profile {self.name!r} has an unusable chain:\n  " + "\n  ".join(problems))

    # -- step lookups ------------------------------------------------------

    @property
    def step_names(self) -> tuple[str, ...]:
        """Every step, in waterfall order."""
        return tuple(s.name for s in self.steps)

    @property
    def levels(self) -> tuple[str, ...]:
        """The level steps, in order."""
        return tuple(s.name for s in self.steps if s.kind is Kind.LEVEL)

    @property
    def deductions(self) -> tuple[str, ...]:
        """The deduction steps, in order."""
        return tuple(s.name for s in self.steps if s.kind is Kind.DEDUCTION)

    @property
    def measures(self) -> tuple[str, ...]:
        """The steps read straight from the data."""
        return tuple(s.name for s in self.steps if s.origin is Origin.MEASURE)

    def step(self, name: str) -> Step:
        """One step by name."""
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(f"{self.name}: no step {name!r}. Have: {list(self.step_names)}")

    def has(self, name: str) -> bool:
        """True when the profile declares this step at all."""
        return any(s.name == name for s in self.steps)

    def steps_at(self, grain: str) -> tuple[str, ...]:
        """Every step known at one grain."""
        return tuple(s.name for s in self.steps if s.grain == grain)

    @property
    def opening(self) -> str:
        """The first level: the top of the waterfall."""
        return self.levels[0]

    @property
    def closing(self) -> str:
        """The last level: the bottom of the waterfall."""
        return self.levels[-1]

    # -- dimension lookups -------------------------------------------------

    def dimension(self, name: str) -> Dimension | None:
        """One dimension by user-facing name or by column name."""
        for d in self.dimensions:
            if name in (d.name, d.column):
                return d
        return None

    @property
    def dimension_columns(self) -> tuple[str, ...]:
        """Fact-table columns every declared dimension resolves to."""
        return tuple(d.column for d in self.dimensions)

    def resolve(self, dimension: str | list[str]) -> list[str]:
        """Map user-facing dimension names onto fact-table columns."""
        names = [dimension] if isinstance(dimension, str) else list(dimension)
        out = []
        for n in names:
            d = self.dimension(n)
            out.append(d.column if d else n)
        return out

    def file_for(self, logical: str, suffix: str = ".csv") -> str:
        """The file name a logical table maps to in a directory-backed source."""
        native = self.tables.get(logical, logical)
        return native if "." in Path(native).name else f"{native}{suffix}"

    def numeric_columns(self, logical: str) -> tuple[str, ...]:
        """Columns of one logical table that must parse as numbers."""
        return tuple(self.semantic_types.get(logical, {}).get("numeric", ()))

    def date_columns(self, logical: str) -> tuple[str, ...]:
        """Columns of one logical table that must parse as dates."""
        return tuple(self.semantic_types.get(logical, {}).get("date", ()))

    def key_safe(self, columns: list[str]) -> bool:
        """True when grouping by these columns cannot split a document."""
        for col in columns:
            if col == self.key:
                continue
            d = self.dimension(col)
            if d is None or not d.key_safe:
                return False
        return True


def validate_chain(steps: tuple[Step, ...]) -> list[str]:
    """Check that a step chain alternates properly and is actually computable."""
    problems: list[str] = []
    if not steps:
        return ["chain is empty"]
    if steps[0].kind is not Kind.LEVEL:
        problems.append(f"chain must open on a level, not {steps[0].name!r}")
    if steps[-1].kind is not Kind.LEVEL:
        problems.append(f"chain must close on a level, not {steps[-1].name!r}")

    for a, b in zip(steps, steps[1:], strict=False):
        if a.kind is b.kind:
            problems.append(f"{a.name!r} and {b.name!r} are both {a.kind.value}s -- levels and deductions must alternate")

    # Walking the chain top to bottom resolves a derived level from the two steps above it,
    # which are already known.
    for i, s in enumerate(steps):
        if s.origin is not Origin.DERIVED:
            continue
        if s.kind is Kind.LEVEL and i < 2:
            problems.append(f"{s.name!r} is derived but has nothing above it to derive from")
        if s.kind is Kind.DEDUCTION:
            below = steps[i + 1] if i + 1 < len(steps) else None
            if below is None:
                problems.append(f"{s.name!r} is a derived deduction with no level below it")
            elif below.origin is Origin.DERIVED:
                problems.append(
                    f"{s.name!r} and {below.name!r} are both derived from each other"
                )
    return problems


def coarsest(*grains: str) -> str:
    """The coarsest of some grains: anything mixed with key grain is key grain."""
    return KEY if KEY in grains else LINE


def inputs_of(steps: tuple[Step, ...], i: int) -> tuple[int, ...]:
    """Indices of the steps a derived step is computed from."""
    if steps[i].kind is Kind.LEVEL:
        return tuple(j for j in (i - 2, i - 1) if j >= 0)
    return tuple(j for j in (i - 1, i + 1) if 0 <= j < len(steps))


def propagate_grain(steps: tuple[Step, ...]) -> tuple[Step, ...]:
    """Give every derived step the coarsest grain among the steps it depends on."""
    out = list(steps)
    for _ in range(len(out)):
        changed = False
        for i, s in enumerate(out):
            if s.origin is not Origin.DERIVED:
                continue
            grain = coarsest(*(out[j].grain for j in inputs_of(tuple(out), i)))
            if grain != s.grain:
                out[i] = Step(s.name, s.kind, s.origin, grain, s.label, s.formula)
                changed = True
        if not changed:
            break
    return tuple(out)
