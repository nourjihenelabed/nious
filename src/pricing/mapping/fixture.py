"""Generate a tiny workbook that has exactly the structure a source mapping declares."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .r2rml import SourceMapping

DEFAULT_ROWS = 3
LINE_ROWS = 6

#: Row counts for the sheets whose grain is finer than "one row per entity".
SHEET_ROWS: dict[str, int] = {
    "19_Sales_Orders": LINE_ROWS,
    "20_Shipments": LINE_ROWS,
    "22_Returns": 2,
    "23_Invoices": LINE_ROWS,
    "02_Data_Dictionary": 8,
}

#: Placeholder vocabularies, so a generated sheet reads like the thing it stands in for rather
#: than like `Col-1, Col-2`.
VOCAB: dict[str, tuple[str, ...]] = {
    "City_AR": ("الخرج", "الرياض", "الدمام"),
    "Customer_Name_AR": ("أسواق الوفرة", "مخابز النخيل", "سوبرماركت الواحة"),
    "Channel": ("Retail", "Wholesale", "HoReCa"),
    "Breed": ("Holstein", "Jersey", "Brown Swiss"),
    "Lactation_Stage": ("Early", "Mid", "Late"),
    "Scenario_ID": ("BASE", "UPSIDE"),
    "Category": ("Herd", "Yield", "Commercial"),
    "Unit": ("SAR", "L/day", "%"),
    "Data_Type": ("string", "date", "decimal"),
    "Update_Frequency": ("daily", "monthly", "on change"),
    "Comparison": ("<=", ">=", "=="),
    "Status": ("PASS", "PASS", "PASS"),
}

_ARABIC_FILLER = ("قيمة تجريبية", "بيان تجريبي", "وصف تجريبي")

_MONEY = re.compile(r"(SAR|Price|Cost|Revenue|Amount|Value|Freight|Capital)", re.I)
_PCT = re.compile(r"(_Pct$|%|Rate$|Margin_Pct)", re.I)
_COUNT = re.compile(r"(Volume|Units|Quantity|Qty|Count|Head|Litres|Liters|Kg)", re.I)
_DATE = re.compile(r"(Date|_From$|_To$|Time$)", re.I)
_FLAG = re.compile(r"_Flag$", re.I)
_ARABIC = re.compile(r"_AR$")

START = pd.Timestamp("2026-01-05")

#: Sheets built by hand rather than from placeholders, because their numbers
#: have to agree with each other for the engine's checks to mean anything.
EXPLICIT_SHEETS = ("24_Financial_Plan", "26_Dashboard", "25_Reconciliations")


@dataclass(frozen=True)
class FixtureSpec:
    """How big a fixture to build, and where to write it."""

    mapping: SourceMapping
    out_path: Path
    seed: int = 20260824
    rows: dict[str, int] | None = None

    def rows_for(self, sheet: str) -> int:
        """Row count for one sheet."""
        override = (self.rows or {}).get(sheet)
        return int(override if override is not None else SHEET_ROWS.get(sheet, DEFAULT_ROWS))


def _prefix(column: str) -> str:
    """A short uppercase stem for an id column: `Sales_Order_Line_ID` -> `SOL`."""
    words = [w for w in column.replace("_ID", "").split("_") if w]
    if not words:
        return "ID"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words).upper()


def _id_columns(mapping: SourceMapping) -> dict[str, str]:
    """Every id-shaped column in the mapping, and the sheet that defines it."""
    owners: dict[str, str] = {}
    for cm in mapping.class_maps:
        candidates = list(cm.identity_columns)
        if not cm.identity_columns:
            candidates += [c for c in cm.property_columns if c.endswith("_ID")]
        for col in candidates:
            owners.setdefault(col, mapping.primary_sheet_for(cm.class_name) or cm.sheet)
    return owners


def _pools(mapping: SourceMapping, spec: FixtureSpec) -> dict[str, list]:
    """One value pool per id column, sized by the sheet that defines it."""
    pools: dict[str, list] = {}
    for col, sheet in _id_columns(mapping).items():
        n = spec.rows_for(sheet)
        if col in VOCAB:
            pools[col] = list(VOCAB[col][:n]) or list(VOCAB[col])
        elif col == "Date_ID":
            pools[col] = [int((START + pd.Timedelta(days=i)).strftime("%Y%m%d")) for i in range(n)]
        else:
            stem = _prefix(col)
            pools[col] = [f"{stem}-{i + 1:03d}" for i in range(n)]
    return pools


def _value(column: str, i: int, rng: np.random.Generator):
    """A placeholder for a column the mapping gives no other meaning."""
    if column in VOCAB:
        options = VOCAB[column]
        return options[i % len(options)]
    if _ARABIC.search(column):
        return f"{_ARABIC_FILLER[i % len(_ARABIC_FILLER)]} {i + 1}"
    if _FLAG.search(column):
        return "Y" if i % 2 == 0 else "N"
    if _DATE.search(column):
        return (START + pd.Timedelta(days=i)).date().isoformat()
    if _PCT.search(column):
        return round(float(rng.uniform(5, 35)), 2)
    if _MONEY.search(column):
        return round(float(rng.uniform(500, 50_000)), 2)
    if _COUNT.search(column):
        return int(rng.integers(20, 500))
    return f"{column.replace('_', ' ')} {i + 1}"


def _generic_sheet(
    mapping: SourceMapping, sheet: str, spec: FixtureSpec, pools: dict[str, list],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Fill one sheet: pools for id columns, placeholders for everything else."""
    columns = mapping.sheet_columns(sheet)
    n = spec.rows_for(sheet)
    data: dict[str, list] = {}
    for col in columns:
        pool = pools.get(col)
        if pool:
            data[col] = [pool[i % len(pool)] for i in range(n)]
        else:
            data[col] = [_value(col, i, rng) for i in range(n)]
    return pd.DataFrame(data, columns=list(columns))


# ---------------------------------------------------------------------------
# The money sheets, built explicitly so the arithmetic is real.
# ---------------------------------------------------------------------------


def _financial_plan(
    mapping: SourceMapping, pools: dict[str, list], rng: np.random.Generator
) -> pd.DataFrame:
    """One row per scenario x SKU x region, internally consistent."""
    columns = list(mapping.sheet_columns("24_Financial_Plan"))
    skus = pools.get("SKU_ID", ["SKU-001"])
    regions = pools.get("Region_ID", ["REG-001"])
    scenarios = pools.get("Scenario_ID", list(VOCAB["Scenario_ID"]))

    rows = []
    for scenario in scenarios:
        uplift = 1.0 if scenario == scenarios[0] else 1.15
        for sku in skus:
            for region in regions:
                volume = int(rng.integers(4_000, 12_000)) * (2 if scenario != scenarios[0] else 1)
                price = round(float(rng.uniform(3.2, 9.5)), 3)
                revenue = round(volume * price * uplift, 2)
                cogs = round(revenue * float(rng.uniform(0.52, 0.71)), 2)
                freight = round(revenue * float(rng.uniform(0.02, 0.06)), 2)
                returns = round(revenue * float(rng.uniform(0.005, 0.03)), 2)
                margin = round(revenue - cogs - freight - returns, 2)
                rows.append(
                    {
                        "Scenario_ID": scenario,
                        "SKU_ID": sku,
                        "Region_ID": region,
                        "Planned_Volume_Units": volume,
                        "Planned_Net_Price_SAR_Per_Unit": price,
                        "Planned_Revenue_SAR": revenue,
                        "Planned_COGS_SAR": cogs,
                        "Planned_Freight_SAR": freight,
                        "Planned_Returns_SAR": returns,
                        "Planned_Contribution_Margin_SAR": margin,
                        "Planned_Contribution_Margin_Pct": round(100 * margin / revenue, 2),
                        "Planned_Working_Capital_SAR": round(revenue * 0.18, 2),
                    }
                )

    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df.columns:
            df[col] = [_value(col, i, rng) for i in range(len(df))]
    return df[columns]


def _dashboard(mapping: SourceMapping, plan: pd.DataFrame) -> pd.DataFrame:
    """The summary row, as the exact sum of the base-scenario plan rows."""
    columns = list(mapping.sheet_columns("26_Dashboard"))
    base = plan[plan["Scenario_ID"] == plan["Scenario_ID"].iloc[0]]
    revenue = round(float(base["Planned_Revenue_SAR"].sum()), 2)
    margin = round(float(base["Planned_Contribution_Margin_SAR"].sum()), 2)
    row = {
        "Volume": int(base["Planned_Volume_Units"].sum()),
        "Revenue": revenue,
        "Product cost": round(float(base["Planned_COGS_SAR"].sum()), 2),
        "Shipping": round(float(base["Planned_Freight_SAR"].sum()), 2),
        "Returns": round(float(base["Planned_Returns_SAR"].sum()), 2),
        "Contribution margin": margin,
        "Margin %": round(100 * margin / revenue, 2) if revenue else None,
    }
    return pd.DataFrame([{c: row.get(c) for c in columns}], columns=columns)


def _reconciliations(mapping: SourceMapping, plan: pd.DataFrame, dash: pd.DataFrame) -> pd.DataFrame:
    """`25_Reconciliations`, stating the two checks the fixture actually satisfies."""
    columns = list(mapping.sheet_columns("25_Reconciliations"))
    base = plan[plan["Scenario_ID"] == plan["Scenario_ID"].iloc[0]]
    identity_gap = float(
        (
            base["Planned_Revenue_SAR"]
            - base["Planned_COGS_SAR"]
            - base["Planned_Freight_SAR"]
            - base["Planned_Returns_SAR"]
            - base["Planned_Contribution_Margin_SAR"]
        ).abs().max()
    )
    rollup_gap = abs(float(base["Planned_Revenue_SAR"].sum()) - float(dash["Revenue"].iloc[0]))

    rows = [
        {
            "Check_ID": "CHK-001",
            "Category": "Financial plan",
            "Test description": "revenue - cogs - freight - returns == contribution margin",
            "Actual value": round(identity_gap, 4),
            "Expected/Limit": 0,
            "Tolerance": 0.01,
            "Comparison": "<=",
            "Status": "PASS" if identity_gap <= 0.01 else "FAIL",
            "Unit": "SAR",
            "Notes": "row-level identity on 24_Financial_Plan",
        },
        {
            "Check_ID": "CHK-002",
            "Category": "Roll-up",
            "Test description": "26_Dashboard revenue == sum of base-scenario plan revenue",
            "Actual value": round(rollup_gap, 4),
            "Expected/Limit": 0,
            "Tolerance": 0.01,
            "Comparison": "<=",
            "Status": "PASS" if rollup_gap <= 0.01 else "FAIL",
            "Unit": "SAR",
            "Notes": "cross-sheet roll-up",
        },
        {
            "Check_ID": "CHK-003",
            "Category": "Grain",
            "Test description": "invoice lines carry a money column",
            "Actual value": 0,
            "Expected/Limit": 1,
            "Tolerance": 0,
            "Comparison": ">=",
            "Status": "FAIL",
            "Unit": "columns",
            "Notes": "23_Invoices maps ids and links only; no amount is exposed at line grain",
        },
    ]
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def build_tables(spec: FixtureSpec) -> dict[str, pd.DataFrame]:
    """Every sheet the mapping declares, as DataFrames, in mapping order."""
    mapping = spec.mapping
    rng = np.random.default_rng(spec.seed)
    pools = _pools(mapping, spec)

    sheets: dict[str, pd.DataFrame] = {}
    for sheet in mapping.sheets:
        if sheet in EXPLICIT_SHEETS:
            continue
        sheets[sheet] = _generic_sheet(mapping, sheet, spec, pools, rng)

    if "24_Financial_Plan" in mapping.sheets:
        sheets["24_Financial_Plan"] = _financial_plan(mapping, pools, rng)
    if "26_Dashboard" in mapping.sheets:
        sheets["26_Dashboard"] = _dashboard(mapping, sheets["24_Financial_Plan"])
    if "25_Reconciliations" in mapping.sheets:
        sheets["25_Reconciliations"] = _reconciliations(
            mapping, sheets["24_Financial_Plan"], sheets["26_Dashboard"]
        )

    return {sheet: sheets[sheet] for sheet in mapping.sheets if sheet in sheets}


def write(spec: FixtureSpec) -> Path:
    """Build the fixture and write it as a workbook, one sheet per logical table."""
    tables = build_tables(spec)
    spec.out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(spec.out_path, engine="openpyxl") as writer:
        for sheet, df in tables.items():
            df.to_excel(writer, sheet_name=sheet[:31], index=False)
    return spec.out_path
