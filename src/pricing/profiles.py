"""The shipped pricing profiles: one per source schema the module knows how to read."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import config
from .graph.source import Source
from .spec import KEY, LINE, Dimension, Kind, Origin, Profile, Step, propagate_grain


@dataclass(frozen=True)
class Extract:
    """What a profile pulls out of a source, before any waterfall arithmetic."""

    lines: pd.DataFrame
    keys: pd.DataFrame
    reference: pd.DataFrame | None = None


# ---------------------------------------------------------------------------
# b2b_industrial
# ---------------------------------------------------------------------------

_B2B_STEPS = propagate_grain(
    (
        Step("list_value", Kind.LEVEL, Origin.MEASURE, LINE, "List value", "quantity x list_price"),
        Step("negotiated_off_list", Kind.DEDUCTION, Origin.DERIVED, LINE, "- negotiated off-list"),
        Step("gross_value", Kind.LEVEL, Origin.MEASURE, LINE, "= Gross (invoiced) value", "line_total"),
        Step("on_invoice_discount", Kind.DEDUCTION, Origin.MEASURE, KEY, "- on-invoice discounts",
             "sum of on-invoice DISCOUNTED_BY edges"),
        Step("invoice_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Invoice price"),
        Step("off_invoice_leakage", Kind.DEDUCTION, Origin.MEASURE, KEY, "- off-invoice leakage",
             "sum of off-invoice DISCOUNTED_BY edges"),
        Step("pocket_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket price"),
        Step("cogs", Kind.DEDUCTION, Origin.MEASURE, LINE, "- COGS", "quantity x unit_cost"),
        Step("pocket_margin", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket margin"),
    )
)

_B2B_DIMENSIONS = (
    Dimension("transaction", "transaction_id", KEY),
    Dimension("customer", "customer_id", KEY, "customer_name"),
    Dimension("channel", "channel_id", KEY, "channel_type"),
    Dimension("region", "region_id", KEY, "region_name"),
    Dimension("segment", "segment", KEY),
    Dimension("month", "period", KEY),
    Dimension("period", "period", KEY),
    Dimension("tier", "tier", KEY),
    Dimension("industry", "industry", KEY),
    Dimension("size", "size", KEY),
    Dimension("country", "country", KEY),
    Dimension("market", "market_type", KEY),
    Dimension("currency", "currency", KEY),
    Dimension("payment_terms", "payment_terms", KEY),
    Dimension("product", "product_id", LINE, "product_name"),
    Dimension("category", "category", LINE),
    Dimension("sub_category", "sub_category", LINE),
    Dimension("family", "product_family", LINE),
)


def _b2b_dimension_columns(src: Source) -> pd.DataFrame:
    """Everything needed to slice the waterfall, joined once, up front."""
    txn = src.table("transactions")[
        ["transaction_id", "date", "customer_id", "channel_id", "payment_terms",
         "gross_amount", "net_amount"]
    ]
    cust = src.table("customers")[
        ["customer_id", "name", "segment", "region_id", "tier", "industry", "size"]
    ].rename(columns={"name": "customer_name"})
    reg = src.table("regions")[
        ["region_id", "name", "currency", "market_type", "country"]
    ].rename(columns={"name": "region_name"})
    chan = src.table("channels")[
        ["channel_id", "type", "cost_to_serve", "commission_rate"]
    ].rename(columns={"type": "channel_type"})

    return (
        txn.merge(cust, on="customer_id", how="left")
        .merge(reg, on="region_id", how="left")
        .merge(chan, on="channel_id", how="left")
    )


def _b2b_key_measures(src: Source, transaction_ids: pd.Index) -> pd.DataFrame:
    """Order-grain concessions, kept at order grain."""
    tdisc = src.table("transaction_discounts").merge(
        src.table("discounts")[["discount_id", "type"]], on="discount_id", how="left"
    )
    on_invoice, off_invoice = config.ON_INVOICE, config.OFF_INVOICE
    names = {on_invoice: "on_invoice_discount", off_invoice: "off_invoice_leakage"}

    amounts = (
        tdisc.pivot_table(
            index="transaction_id",
            columns="type",
            values="discount_amount_applied",
            aggfunc=lambda s: s.sum(min_count=1),
        )
        .reindex(index=transaction_ids, columns=[on_invoice, off_invoice])
        .rename(columns=names)
    )

    unknown = (
        tdisc.assign(_bad=tdisc["discount_amount_applied"].isna())
        .pivot_table(index="transaction_id", columns="type", values="_bad", aggfunc="sum")
        .reindex(index=transaction_ids, columns=[on_invoice, off_invoice])
        .fillna(0)
        .rename(columns={k: f"n_unknown_{v}" for k, v in names.items()})
    )

    edges = (
        tdisc.pivot_table(
            index="transaction_id", columns="type", values="discount_id", aggfunc="size"
        )
        .reindex(index=transaction_ids, columns=[on_invoice, off_invoice])
        .fillna(0)
    )
    # No DISCOUNTED_BY edge of a kind means no concession of that kind: a true
    # zero, distinct from an edge whose amount nobody recorded.
    for kind, col in names.items():
        amounts.loc[edges[kind] == 0, col] = 0.0

    out = amounts.join(unknown)
    out.index.name = "transaction_id"
    return out.reset_index()


def _b2b_extract(src: Source) -> Extract:
    """Line-grain measures and order-grain concessions for the distributor schema."""
    dims = _b2b_dimension_columns(src)
    lines = src.table("transaction_lines")
    prods = src.table("products")[
        ["product_id", "name", "category", "sub_category", "list_price", "unit_cost",
         "product_family"]
    ].rename(columns={"name": "product_name"})

    f = lines.merge(prods, on="product_id", how="left").merge(dims, on="transaction_id", how="left")
    f["list_value"] = f["quantity"] * f["list_price"]
    f["cogs"] = f["quantity"] * f["unit_cost"]
    f["gross_value"] = f["line_total"]
    f["period"] = f["date"].dt.to_period("M").astype(str)

    keys = _b2b_key_measures(src, pd.Index(f["transaction_id"].unique(), name="transaction_id"))
    return Extract(lines=f, keys=keys)


def _b2b_validate(src: Source, report):
    """Entry-point checks for the distributor schema."""
    from .graph.validation import _b2b_checks

    return _b2b_checks(src, report)


def _b2b_reasons(view: pd.DataFrame, _profile: Profile) -> pd.Series:
    """Why a transaction was excluded, in the vocabulary of this schema."""
    return pd.Series(
        np.select(
            [
                view["certified"],
                view["list_price"].isna(),
                view["unit_cost"].isna(),
                (view["n_unknown_on_invoice_discount"] > 0)
                | (view["n_unknown_off_invoice_leakage"] > 0),
            ],
            [
                "",
                "product has no list_price",
                "product has no unit_cost",
                "discount applied with unknown amount",
            ],
            default="another line in this transaction is incomplete",
        ),
        index=view.index,
    )


def _b2b_flags(w) -> pd.DataFrame:
    """Data-quality findings for the distributor schema, with blast radius."""
    view = w.line_view()

    def n_txn(mask) -> int:
        """Distinct transactions touched by a boolean mask."""
        return int(view.loc[mask, "transaction_id"].nunique())

    unknown = view["n_unknown_on_invoice_discount"] + view["n_unknown_off_invoice_leakage"]
    above_list = view["unit_price"] > view["list_price"]
    negative_margin = w.keys["pocket_margin"] < 0

    return pd.DataFrame(
        [
            {
                "flag": "product missing list_price",
                "n_lines": int(view["list_price"].isna().sum()),
                "n_transactions": n_txn(view["list_price"].isna()),
                "consequence": "list value & price realisation UNAVAILABLE",
            },
            {
                "flag": "product missing unit_cost",
                "n_lines": int(view["unit_cost"].isna().sum()),
                "n_transactions": n_txn(view["unit_cost"].isna()),
                "consequence": "COGS & pocket margin UNAVAILABLE",
            },
            {
                "flag": "discount applied with unknown amount",
                "n_lines": int((unknown > 0).sum()),
                "n_transactions": n_txn(unknown > 0),
                "consequence": "leakage understated; not certified",
            },
            {
                "flag": "line priced above list",
                "n_lines": int(above_list.sum()),
                "n_transactions": n_txn(above_list),
                "consequence": "negative discount -- check list price maintenance",
            },
            {
                "flag": "negative pocket margin",
                "n_lines": int(
                    view["transaction_id"]
                    .isin(w.keys.loc[negative_margin, "transaction_id"])
                    .sum()
                ),
                "n_transactions": int(negative_margin.sum()),
                "consequence": "selling below cost -- a real finding, not an error",
            },
        ]
    )


B2B_INDUSTRIAL = Profile(
    name="b2b_industrial",
    entity="transaction line",
    key="transaction_id",
    steps=_B2B_STEPS,
    dimensions=_B2B_DIMENSIONS,
    tables=dict(config.CSV_FILES),
    semantic_types={
        "transactions": {
            "numeric": ("gross_amount", "net_amount", "quantity", "unit_price"),
            "date": ("date",),
        },
        "transaction_lines": {"numeric": ("quantity", "unit_price", "line_total")},
        "transaction_discounts": {"numeric": ("discount_amount_applied",)},
        "discounts": {"numeric": ("rate", "amount"), "date": ("valid_from", "valid_to")},
        "customers": {"numeric": ("tenure_years",)},
        "products": {"numeric": ("list_price", "unit_cost")},
        "channels": {"numeric": ("cost_to_serve", "commission_rate")},
    },
    build_facts=_b2b_extract,
    uncertified_reasons=_b2b_reasons,
    quality_flags=_b2b_flags,
    validate_source=_b2b_validate,
    currency_column="currency",
    date_column="date",
    quantity_column="quantity",
    unit_price_column="unit_price",
    description="Synthetic B2B industrial distributor: list price to pocket margin.",
)


# ---------------------------------------------------------------------------
# dairy_al_kharj
# ---------------------------------------------------------------------------

_DAIRY_STEPS = propagate_grain(
    (
        Step("gross_value", Kind.LEVEL, Origin.MEASURE, LINE, "Planned revenue",
             "24_Financial_Plan.Planned_Revenue_SAR"),
        Step("off_invoice_leakage", Kind.DEDUCTION, Origin.MEASURE, LINE, "- freight and returns",
             "Planned_Freight_SAR + Planned_Returns_SAR"),
        Step("pocket_price", Kind.LEVEL, Origin.DERIVED, LINE, "= Net delivered revenue"),
        Step("cogs", Kind.DEDUCTION, Origin.MEASURE, LINE, "- product cost",
             "24_Financial_Plan.Planned_COGS_SAR"),
        Step("pocket_margin", Kind.LEVEL, Origin.DERIVED, LINE, "= Contribution margin"),
    )
)

_DAIRY_DIMENSIONS = (
    Dimension("plan_row", "transaction_id", KEY),
    Dimension("product", "product_id", KEY, description="SKU_ID"),
    Dimension("region", "region_id", KEY, description="Region_ID"),
    Dimension("scenario", "scenario_id", KEY, description="Scenario_ID"),
    Dimension("currency", "currency", KEY),
)

#: The mapping puts money on `24_Financial_Plan` only.
DAIRY_ABSENT_STEPS: dict[str, str] = {
    "list_value": "no list price is mapped on any sheet; 08_Products exposes SKU_ID only",
    "negotiated_off_list": "requires list_value, which the mapping does not expose",
    "on_invoice_discount": "no discount or concession entity exists in the mapping",
    "invoice_price": "requires on_invoice_discount, which the mapping does not expose",
}

_DAIRY_MONEY = {
    "gross_value": ["Planned_Revenue_SAR"],
    "cogs": ["Planned_COGS_SAR"],
    "off_invoice_leakage": ["Planned_Freight_SAR", "Planned_Returns_SAR"],
}


def _dairy_extract(src: Source) -> Extract:
    """Plan-grain measures for the dairy mapping."""
    plan = src.table("financial_plan").copy()

    def col(name: str) -> pd.Series:
        """A plan column as numbers, or all-NaN when the sheet does not carry it."""
        if name not in plan.columns:
            return pd.Series(np.nan, index=plan.index)
        return pd.to_numeric(plan[name], errors="coerce")

    f = pd.DataFrame(index=plan.index)
    f["scenario_id"] = plan.get("Scenario_ID", pd.Series("BASE", index=plan.index)).astype(str)
    f["product_id"] = plan.get("SKU_ID", pd.Series(pd.NA, index=plan.index)).astype(str)
    f["region_id"] = plan.get("Region_ID", pd.Series(pd.NA, index=plan.index)).astype(str)
    f["transaction_id"] = f["scenario_id"] + "|" + f["product_id"] + "|" + f["region_id"]
    f["currency"] = "SAR"
    f["quantity"] = col("Planned_Volume_Units")
    f["unit_price"] = col("Planned_Net_Price_SAR_Per_Unit")

    for step, sources in _DAIRY_MONEY.items():
        parts = [col(c) for c in sources]
        total = parts[0]
        for extra in parts[1:]:
            total = total.add(extra)
        f[step] = total

    reference = pd.DataFrame(
        {
            "transaction_id": f["transaction_id"],
            "reported_pocket_margin": col("Planned_Contribution_Margin_SAR"),
        }
    )

    keys = pd.DataFrame({"transaction_id": f["transaction_id"].unique()})
    return Extract(lines=f, keys=keys, reference=reference)


def _dairy_validate(src: Source, report):
    """Entry-point checks for the dairy mapping."""
    from .graph.validation import Finding, Severity

    add = report.findings.append
    plan = src.table("financial_plan")

    required = [c for cols in _DAIRY_MONEY.values() for c in cols]
    absent = [c for c in required if c not in plan.columns]
    if absent:
        add(
            Finding(
                Severity.BLOCKING,
                "plan measures",
                f"24_Financial_Plan is missing {', '.join(absent)}; nothing can be computed",
                len(absent),
            )
        )
        return report

    for label, child, parent_table, parent_col in (
        ("plan -> product", "SKU_ID", "products", "SKU_ID"),
        ("plan -> region", "Region_ID", "regions", "Region_ID"),
    ):
        if child not in plan.columns:
            continue
        parent = src.table(parent_table)
        if parent_col not in parent.columns:
            continue
        unresolved = plan.loc[~plan[child].isin(set(parent[parent_col])), child]
        if len(unresolved):
            add(
                Finding(
                    Severity.BLOCKING,
                    "referential integrity",
                    f"{label} has unresolved references, e.g. {list(unresolved.unique()[:3])}",
                    len(unresolved),
                )
            )

    if "Planned_Contribution_Margin_SAR" in plan.columns:
        stated = pd.to_numeric(plan["Planned_Contribution_Margin_SAR"], errors="coerce")
        computed = (
            pd.to_numeric(plan["Planned_Revenue_SAR"], errors="coerce")
            - pd.to_numeric(plan["Planned_COGS_SAR"], errors="coerce")
            - pd.to_numeric(plan["Planned_Freight_SAR"], errors="coerce")
            - pd.to_numeric(plan["Planned_Returns_SAR"], errors="coerce")
        )
        broken = int(((stated - computed).abs() > 0.01).sum())
        if broken:
            add(
                Finding(
                    Severity.BLOCKING,
                    "plan identity",
                    "Planned_Contribution_Margin_SAR does not equal revenue less "
                    "cogs, freight and returns",
                    broken,
                )
            )

    for step, sources in _DAIRY_MONEY.items():
        for col in sources:
            negative = int((pd.to_numeric(plan[col], errors="coerce") < 0).sum())
            if negative:
                add(
                    Finding(
                        Severity.BLOCKING, "semantic type",
                        f"{col} has negative values (feeds {step})", negative,
                    )
                )

    add(
        Finding(
            Severity.WARNING,
            "money grain",
            "the mapping exposes amounts on 24_Financial_Plan only; 23_Invoices maps "
            "InvoiceLine ids and links but no measure, so the waterfall cannot reach "
            "invoice-line grain and no customer dimension is available",
            len(src.table("invoices")),
        )
    )

    for step, reason in DAIRY_ABSENT_STEPS.items():
        add(Finding(Severity.WARNING, "absent step", f"{step}: {reason}"))

    return report


DAIRY_AL_KHARJ = Profile(
    name="dairy_al_kharj",
    entity="financial plan row",
    key="transaction_id",
    steps=_DAIRY_STEPS,
    dimensions=_DAIRY_DIMENSIONS,
    tables={
        "assumptions": "01_Assumptions",
        "data_dictionary": "02_Data_Dictionary",
        "calendar": "03_Calendar",
        "regions": "04_Regions",
        "products": "08_Products",
        "customers": "09_Customers",
        "sales_orders": "19_Sales_Orders",
        "shipments": "20_Shipments",
        "returns": "22_Returns",
        "invoices": "23_Invoices",
        "financial_plan": "24_Financial_Plan",
        "reconciliations": "25_Reconciliations",
        "dashboard": "26_Dashboard",
    },
    semantic_types={
        "financial_plan": {
            "numeric": (
                "Planned_Volume_Units", "Planned_Net_Price_SAR_Per_Unit",
                "Planned_Revenue_SAR", "Planned_COGS_SAR", "Planned_Freight_SAR",
                "Planned_Returns_SAR", "Planned_Contribution_Margin_SAR",
                "Planned_Contribution_Margin_Pct", "Planned_Working_Capital_SAR",
            )
        },
        "dashboard": {
            "numeric": (
                "Volume", "Revenue", "Product cost", "Shipping", "Returns",
                "Contribution margin", "Margin %",
            )
        },
        "sales_orders": {"date": ("Order_Date",)},
        "invoices": {"date": ("Invoice_Date",)},
        "shipments": {"date": ("Ship_Date",)},
        "returns": {"date": ("Return_Date",)},
    },
    build_facts=_dairy_extract,
    validate_source=_dairy_validate,
    absent_steps=DAIRY_ABSENT_STEPS,
    currency_column="currency",
    date_column=None,
    quantity_column="quantity",
    unit_price_column="unit_price",
    description="Al Kharj dairy model read through axiom-source-mapping; plan grain.",
)


# ---------------------------------------------------------------------------
# axiom_poc
# ---------------------------------------------------------------------------

#: The graph records its own waterfall. `PriceComponent` nodes carry a
#: `step_name`, a `step_type` of level or deduction, an `on_invoice` flag and an
#: `applies_to_level` naming the grain the component was posted at. So the chain
#: below is not an interpretation of the data -- it is a transcription of it, and
#: the only editorial decisions are the two stated here.
#:
#: **Which band a concession belongs to** is read off the component's own
#: `on_invoice` flag, never off its name. Freight is a delivery charge rather
#: than a discount, but this graph posts it on-invoice, so it lands in the
#: on-invoice band. Rename that flag upstream and this profile follows.
#:
#: **Grain.** Deductions arrive at three grains: the order line, the order, and
#: the customer-quarter. The first two belong to a document and roll up into it.
#: The third does not: a rebate accrued against a customer's quarter spans orders
#: that are not all in this book, and attributing it to any one of them would be
#: allocation. So the customer-period components are not in the chain at all, and
#: `_axiom_validate` reports what they are worth instead of burying them.
_AXIOM_STEPS = propagate_grain(
    (
        Step("list_value", Kind.LEVEL, Origin.MEASURE, LINE, "List value",
             "PriceComponent step_name='list_price' at line grain"),
        Step("negotiated_off_list", Kind.DEDUCTION, Origin.DERIVED, LINE,
             "- negotiated off-list"),
        Step("gross_value", Kind.LEVEL, Origin.MEASURE, LINE, "= Gross (ordered) value",
             "OrderLine.line_value"),
        Step("on_invoice_discount", Kind.DEDUCTION, Origin.MEASURE, KEY,
             "- on-invoice concessions",
             "PriceComponent on_invoice=true: invoice_discount and freight, at line "
             "and order grain"),
        Step("invoice_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Invoice price"),
        Step("off_invoice_leakage", Kind.DEDUCTION, Origin.MEASURE, LINE,
             "- off-invoice leakage",
             "PriceComponent on_invoice=false: payment_discount and volume_rebate, "
             "at line grain"),
        Step("pocket_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket price"),
        Step("cogs", Kind.DEDUCTION, Origin.MEASURE, LINE, "- COGS",
             "quantity x ProductCostPeriod.unit_cost in force on the order date"),
        Step("pocket_margin", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket margin"),
    )
)

_AXIOM_DIMENSIONS = (
    Dimension("order", "order_id", KEY),
    Dimension("customer", "customer_id", KEY, "legal_name"),
    Dimension("segment", "customer_type", KEY, description="TradeCustomer.customer_type"),
    Dimension("industry", "industry_code", KEY),
    Dimension("account_class", "account_class", KEY,
              description="CustomerClassPeriod in force on the order date"),
    Dimension("channel", "channel_id", KEY, "channel_type"),
    Dimension("region", "region", KEY),
    Dimension("city", "city", KEY),
    Dimension("period", "period", KEY),
    Dimension("month", "period", KEY),
    Dimension("currency", "currency_code", KEY),
    Dimension("product", "product_id", LINE, "product_name"),
    Dimension("family", "family", LINE),
    Dimension("product_line", "product_line", LINE),
    Dimension("category", "category_code", LINE),
    Dimension("grade", "grade_tier", LINE),
    Dimension("stage", "stage", LINE,
              description="ProductStagePeriod in force on the order date"),
)

#: The two deduction bands, and which `on_invoice` value puts a component in each.
_AXIOM_BANDS = {True: "on_invoice_discount", False: "off_invoice_leakage"}


def _flag(values: pd.Series) -> pd.Series:
    """A graph boolean, which arrives as the strings "true" and "false"."""
    return values.astype(str).str.strip().str.lower().eq("true")


def _as_of(
    periods: pd.DataFrame,
    facts: pd.DataFrame,
    key: str,
    date_column: str,
    valid_from: str = "valid_from",
    valid_to: str | None = None,
) -> pd.DataFrame:
    """The period row in force on each fact's date, or nothing where none is."""
    dates = facts[[key, date_column]].dropna(subset=[key]).drop_duplicates()
    joined = dates.merge(periods, on=key, how="left")

    in_force = joined[valid_from].isna() | (joined[valid_from] <= joined[date_column])
    if valid_to and valid_to in joined.columns:
        in_force &= joined[valid_to].isna() | (joined[date_column] <= joined[valid_to])
    joined = joined[in_force]

    # Latest opening date wins, which is what "in force" means when periods overlap.
    return (
        joined.sort_values(valid_from)
        .groupby([key, date_column], as_index=False, dropna=False)
        .last()
    )


def _axiom_bands(part: pd.DataFrame, ids: pd.Index) -> pd.DataFrame:
    """Deduction totals per band for one grain, plus a count of unusable ones."""
    deductions = part[part["step_type"].astype(str) == "deduction"].copy()
    deductions["_band"] = _flag(deductions["on_invoice"]).map(_AXIOM_BANDS)
    deductions["_deduction"] = -pd.to_numeric(deductions["amount"], errors="coerce")

    columns = list(_AXIOM_BANDS.values())
    amounts = deductions.pivot_table(
        index="applies_to_id", columns="_band", values="_deduction",
        aggfunc=lambda s: s.sum(min_count=1),
    ).reindex(index=ids, columns=columns)

    posted = deductions.pivot_table(
        index="applies_to_id", columns="_band", values="component_id", aggfunc="size"
    ).reindex(index=ids, columns=columns).fillna(0)

    unknown = (
        deductions.assign(_bad=deductions["_deduction"].isna())
        .pivot_table(index="applies_to_id", columns="_band", values="_bad", aggfunc="sum")
        .reindex(index=ids, columns=columns)
        .fillna(0)
        .rename(columns={c: f"n_unknown_{c}" for c in columns})
    )

    for band in columns:
        amounts.loc[posted[band] == 0, band] = 0.0

    out = amounts.join(unknown)
    out.index.name = ids.name
    return out.reset_index()


def _rate_on(fx: pd.DataFrame, currency: pd.Series, dates: pd.Series, target: str) -> pd.Series:
    """The rate from each row's currency into `target` in force on its date."""
    frame = pd.DataFrame({"currency": currency.astype(str).values, "date": dates.values})
    out = pd.Series(np.where(frame["currency"] == target, 1.0, np.nan), index=currency.index)
    pending = frame[frame["currency"] != target].drop_duplicates()
    if pending.empty or fx.empty:
        return out
    rates = pending.merge(
        fx[fx["to_currency"] == target].rename(columns={"from_currency": "currency"}),
        on="currency", how="left",
    )
    rates = rates[
        (rates["valid_from"] <= rates["date"])
        & (rates["valid_to"].isna() | (rates["date"] < rates["valid_to"]))
    ]
    lookup = rates.groupby(["currency", "date"])["rate"].last()
    keys = list(zip(frame["currency"], frame["date"], strict=True))
    found = pd.Series([lookup.get(k, np.nan) for k in keys], index=currency.index)
    return out.fillna(found)


def _axiom_in_currency(src: Source, orders, lines, components, target: str):
    """Every amount on an order restated in `target`, at the rate on the order's date."""
    fx = _axiom_fx(src)
    orders = orders.copy()
    orders["order_currency"] = orders["currency_code"]
    rate = _rate_on(fx, orders["currency_code"], orders["order_date"], target)

    by_order = orders.set_index("order_id")
    lines = lines.copy()
    line_rate = lines["order_id"].map(rate.set_axis(orders["order_id"]))
    for col in ("line_value", "unit_price_recorded"):
        lines[col] = lines[col] * line_rate

    # A component reaches its order through its line, or names the order itself.
    order_of = pd.concat(
        [
            lines.set_index("order_line_id")["order_id"],
            pd.Series(by_order.index, index=by_order.index),
        ]
    )
    components = components.copy()
    owner = components["applies_to_id"].map(order_of)
    when = owner.map(by_order["order_date"])
    on_order = owner.notna()
    comp_rate = _rate_on(
        fx, components.loc[on_order, "currency_code"], when[on_order], target
    )
    components.loc[on_order, "amount"] = components.loc[on_order, "amount"] * comp_rate
    components.loc[on_order, "currency_code"] = target

    orders["fx_rate_to_" + target.lower()] = rate.values
    orders["currency_code"] = np.where(rate.notna(), target, orders["currency_code"])
    return orders, lines, components


def _axiom_extract(
    src: Source,
    in_line_value: tuple[str, ...] = (),
    key_bands: tuple[str, ...] = ("on_invoice_discount",),
    currency: str | None = None,
) -> Extract:
    """Line-grain measures, and each band in `key_bands` at the order grain it needs."""
    orders = src.table("orders")
    lines = src.table("order_lines")
    products = src.table("products")
    customers = src.table("customers")
    channels = src.table("channels")
    opportunities = src.table("opportunities")
    components = src.table("price_components")
    if currency:
        orders, lines, components = _axiom_in_currency(src, orders, lines, components, currency)
    components = components[~components["step_name"].astype(str).isin(in_line_value)]

    # The order reaches its channel through the opportunity it fulfils; there is
    # no edge straight from one to the other.
    header = (
        orders.merge(
            opportunities[["opportunity_id", "channel_id", "is_tender"]],
            on="opportunity_id", how="left",
        )
        .merge(
            channels[["channel_id", "type", "typical_drop_size"]]
            .rename(columns={"type": "channel_type"}),
            on="channel_id", how="left",
        )
        .merge(
            customers[["customer_id", "legal_name", "customer_type", "industry_code",
                       "region", "city", "credit_terms_days", "delivery_point_count"]],
            on="customer_id", how="left",
        )
    )
    header["period"] = header["order_date"].dt.to_period("M").astype(str)

    account_class = _as_of(
        src.table("customer_classes")[
            ["customer_id", "valid_from", "valid_to", "account_class",
             "price_adjustment_factor"]
        ],
        header, "customer_id", "order_date", valid_to="valid_to",
    )
    header = header.merge(account_class, on=["customer_id", "order_date"], how="left")

    f = lines.merge(
        products[["product_id", "name", "family", "product_line", "category_code",
                  "grade_tier", "unit_of_measure", "shelf_life_days"]]
        .rename(columns={"name": "product_name"}),
        on="product_id", how="left",
    ).merge(header, on="order_id", how="left")

    for periods, columns, valid_to in (
        (src.table("list_prices"), ["product_id", "valid_from", "list_price"], None),
        (src.table("product_costs"), ["product_id", "valid_from", "unit_cost"], None),
        (src.table("product_stages"),
         ["product_id", "valid_from", "valid_to", "stage"], "valid_to"),
    ):
        picked = _as_of(periods[columns], f, "product_id", "order_date", valid_to=valid_to)
        f = f.merge(
            picked.drop(columns=[c for c in ("valid_from", "valid_to") if c in picked]),
            on=["product_id", "order_date"], how="left",
        )

    level = components["applies_to_level"].astype(str)
    at_line = components[level == "line"]
    at_order = components[level == "order"]

    line_ids = pd.Index(f["order_line_id"], name="order_line_id")
    line_bands = _axiom_bands(at_line, line_ids)

    recorded_list = (
        at_line[at_line["step_name"].astype(str) == "list_price"]
        .groupby("applies_to_id")["amount"]
        .agg(lambda s: s.sum(min_count=1))
    )

    f["list_value"] = f["order_line_id"].map(recorded_list)
    f["gross_value"] = pd.to_numeric(f["line_value"], errors="coerce")
    f["cogs"] = f["quantity"] * f["unit_cost"]
    f = f.merge(line_bands, on="order_line_id", how="left")

    # A band with components posted against the whole order is known only per order: line-grain
    # components rolled up (free of assumptions) plus the order's own.
    per_order = f.groupby("order_id", dropna=False).agg(
        **{
            col: (col, (lambda s: s.sum(min_count=1)) if not col.startswith("n_") else "sum")
            for band in key_bands
            for col in (band, f"n_unknown_{band}")
        }
    )
    order_bands = _axiom_bands(
        at_order, pd.Index(per_order.index, name="order_id")
    ).set_index("order_id")
    for band in key_bands:
        for col in (band, f"n_unknown_{band}"):
            per_order[col] = per_order[col].add(order_bands[col])
    keys = per_order.reset_index()
    f = f.drop(columns=[c for band in key_bands for c in (band, f"n_unknown_{band}")])

    reference = pd.DataFrame(
        {
            "order_line_id": f["order_line_id"],
            "order_id": f["order_id"],
            "reported_list_value": f["list_value"],
            "computed_list_value": f["quantity"] * f["list_price"],
            "reported_line_value": f["gross_value"],
            "computed_line_value": f["quantity"] * f["unit_price_recorded"],
        }
    )
    return Extract(lines=f, keys=keys, reference=reference)


def _axiom_reasons(view: pd.DataFrame, _profile: Profile) -> pd.Series:
    """Why an order was excluded, named by cause rather than by missing column."""
    return pd.Series(
        np.select(
            [
                view["certified"],
                view["list_value"].isna(),
                view["unit_cost"].isna(),
                view["gross_value"].isna(),
                view["n_unknown_on_invoice_discount"] > 0,
                view["n_unknown_off_invoice_leakage"] > 0,
            ],
            [
                "",
                "no list_price component posted against this line",
                "product has no cost period covering the order date",
                "order line records no line_value",
                "on-invoice component posted with no amount",
                "off-invoice component posted with no amount",
            ],
            default="another line in this order is incomplete",
        ),
        index=view.index,
    )


def _axiom_flags(w) -> pd.DataFrame:
    """Data-quality findings for the PoC graph, with blast radius."""
    view = w.line_view()

    def n_orders(mask) -> int:
        """Distinct orders touched by a boolean mask."""
        return int(view.loc[mask, "order_id"].nunique())

    no_cost = view["unit_cost"].isna()
    no_list = view["list_value"].isna()
    unknown = view["n_unknown_on_invoice_discount"] + view["n_unknown_off_invoice_leakage"]
    above_list = pd.to_numeric(view["unit_price_recorded"], errors="coerce") > pd.to_numeric(
        view["list_price"], errors="coerce"
    )
    negative_margin = w.keys["pocket_margin"] < 0

    return pd.DataFrame(
        [
            {
                "flag": "no list_price component on the line",
                "n_lines": int(no_list.sum()),
                "n_transactions": n_orders(no_list),
                "consequence": "list value & price realisation UNAVAILABLE",
            },
            {
                "flag": "no cost period covering the order date",
                "n_lines": int(no_cost.sum()),
                "n_transactions": n_orders(no_cost),
                "consequence": "COGS & pocket margin UNAVAILABLE",
            },
            {
                "flag": "price component posted with no amount",
                "n_lines": int((unknown > 0).sum()),
                "n_transactions": n_orders(unknown > 0),
                "consequence": "leakage understated; not certified",
            },
            {
                "flag": "line priced above list",
                "n_lines": int(above_list.sum()),
                "n_transactions": n_orders(above_list),
                "consequence": "negative discount -- check list price maintenance",
            },
            {
                "flag": "promotional line",
                "n_lines": int(_flag(view["is_promotional"]).sum()),
                "n_transactions": n_orders(_flag(view["is_promotional"])),
                "consequence": "priced under a campaign, not a negotiation",
            },
            {
                "flag": "negative pocket margin",
                "n_lines": int(
                    view["order_id"].isin(w.keys.loc[negative_margin, "order_id"]).sum()
                ),
                "n_transactions": int(negative_margin.sum()),
                "consequence": "selling below cost -- a real finding, not an error",
            },
        ]
    )


def _axiom_validate(
    src: Source,
    report,
    in_line_value: tuple[str, ...] = (),
    off_invoice_grain: str = LINE,
    currency: str | None = None,
):
    """Entry-point checks for the PoC graph."""
    from .graph.validation import Finding, Severity

    add = report.findings.append
    components = src.table("price_components")
    orders = src.table("orders")
    lines = src.table("order_lines")

    # A backend reading the graph itself can check things a table cannot see: whether the export
    # is whole, whether every edge lands somewhere, and whether the denormalised foreign keys
    # still agree with the edges beside them.
    if hasattr(src, "graph_integrity"):
        for finding in src.graph_integrity():
            add(finding)

    for label, child, child_col, parent_table, parent_col in (
        ("order line -> order", lines, "order_id", "orders", "order_id"),
        ("order line -> product", lines, "product_id", "products", "product_id"),
        ("order -> customer", orders, "customer_id", "customers", "customer_id"),
        ("order -> opportunity", orders, "opportunity_id", "opportunities", "opportunity_id"),
    ):
        parent = src.table(parent_table)
        unresolved = child.loc[~child[child_col].isin(set(parent[parent_col])), child_col]
        if len(unresolved):
            add(
                Finding(
                    Severity.BLOCKING, "broken links",
                    f"{label} points to records that do not exist, "
                    f"e.g. {list(unresolved.unique()[:3])}",
                    len(unresolved),
                )
            )

    level = components["applies_to_level"].astype(str)
    deduction = components["step_type"].astype(str) == "deduction"
    off_invoice = ~_flag(components["on_invoice"])

    # A chain that declares the off-invoice band at line grain cannot read an off-invoice
    # concession posted against an order: that money would be dropped rather than deducted, so
    # this refuses to run instead.
    stranded = components[deduction & off_invoice & (level == "order")]
    if len(stranded) and off_invoice_grain == LINE:
        add(
            Finding(
                Severity.BLOCKING, "money that would be lost",
                f"after-invoice amounts worth {stranded['amount'].sum():,.2f} are recorded "
                "per order, but this profile reads after-invoice amounts per product line "
                "only, so they would be dropped. The profile has to read them per order "
                "(the axiom_v2 profile does).",
                len(stranded),
            )
        )

    # A declared composition is checked, never assumed: if `line_value` is not the list price
    # plus the components said to be inside it, the discounts would be deducted twice or not at
    # all.
    if in_line_value:
        at_line = components[level == "line"]
        parts = at_line[
            (at_line["step_name"].astype(str) == "list_price")
            | at_line["step_name"].astype(str).isin(in_line_value)
        ]
        composed = parts.groupby("applies_to_id")["amount"].sum(min_count=1)
        recorded = lines.set_index("order_line_id")["line_value"]
        gap = (recorded - composed.reindex(recorded.index)).abs()
        broken = recorded.index[~(gap <= 0.01)]
        if len(broken):
            add(
                Finding(
                    Severity.BLOCKING, "line amounts do not add up",
                    "each line amount should equal its list price plus "
                    f"{' and '.join(n.replace('_', ' ') for n in in_line_value)}, "
                    f"but {len(broken)} line(s) differ by more than 0.01, "
                    f"e.g. {list(broken[:3])}",
                    len(broken),
                )
            )

    accrued = components[deduction & (level == "customer_period")]
    if len(accrued):
        worth = f"{accrued['amount'].sum():,.2f}"
        if currency:
            # Converted at the rate on the day each rebate was posted, so the
            # total is one currency rather than euros and riyals added together.
            rate = _rate_on(_axiom_fx(src), accrued["currency_code"], accrued["posted_at"], currency)
            worth = f"{(accrued['amount'] * rate).sum():,.2f} {currency}"
        add(
            Finding(
                Severity.WARNING, "rebates outside the waterfall",
                f"{len(accrued)} rebates worth {worth} are recorded "
                "per customer per quarter, not per order, so they cannot be placed on any "
                "one order. They are real money lost and are reported next to the "
                "waterfall, not split across orders",
                len(accrued),
            )
        )

    estimated = components[deduction & _flag(components["is_estimated"])]
    if len(estimated):
        add(
            Finding(
                Severity.WARNING, "estimates, not final amounts",
                f"{len(estimated)} deductions are marked as estimates that have not been "
                "settled yet",
                len(estimated),
            )
        )

    orphan_lines = int((~lines["order_id"].isin(set(orders["order_id"]))).sum())
    unfulfilled = int(len(src.table("opportunities")) - orders["opportunity_id"].nunique())
    if unfulfilled:
        add(
            Finding(
                Severity.WARNING, "deals without an order",
                f"{unfulfilled} deals did not end in an order; the waterfall covers the "
                f"{len(orders)} that did. The win-probability model learns from the quotes "
                "and outcomes of all of them",
                unfulfilled,
            )
        )
    if orphan_lines:
        add(
            Finding(
                Severity.BLOCKING, "broken links",
                "order lines point to an order that is not in the data", orphan_lines,
            )
        )

    currencies = orders["currency_code"].dropna().unique()
    if len(currencies) > 1 and currency:
        rate = _rate_on(_axiom_fx(src), orders["currency_code"], orders["order_date"], currency)
        missing = int(rate.isna().sum())
        foreign = int((orders["currency_code"] != currency).sum())
        add(
            Finding(
                Severity.WARNING, "more than one currency",
                f"{foreign} orders are not in {currency} "
                f"({', '.join(sorted(c for c in currencies if c != currency))}); their amounts "
                f"are converted to {currency} at the graph's rate for each order's date"
                + (
                    f". {missing} orders have no rate for their date and are left out"
                    if missing else ""
                ),
                foreign,
            )
        )
    elif len(currencies) > 1:
        add(
            Finding(
                Severity.WARNING, "more than one currency",
                f"orders are in {len(currencies)} currencies ({', '.join(sorted(currencies))}); "
                "totals that would mix them are left blank, because the waterfall does not "
                "convert currencies",
                len(currencies),
            )
        )
    return report


AXIOM_POC = Profile(
    name="axiom_poc",
    entity="order line",
    key="order_id",
    steps=_AXIOM_STEPS,
    dimensions=_AXIOM_DIMENSIONS,
    # A profile's table map holds whatever the source natively calls the table, and in a graph
    # that is the node label.
    tables={
        "orders": "Order",
        "order_lines": "OrderLine",
        "price_components": "PriceComponent",
        "products": "SellableProduct",
        "list_prices": "ListPricePeriod",
        "product_costs": "ProductCostPeriod",
        "product_stages": "ProductStagePeriod",
        "customers": "TradeCustomer",
        "customer_classes": "CustomerClassPeriod",
        "customer_periods": "CustomerPeriod",
        "channels": "Channel",
        "opportunities": "Opportunity",
        "quotes": "Quote",
        "deal_outcomes": "DealOutcome",
        "agents": "SalesAgent",
    },
    semantic_types={
        "orders": {
            "date": ("order_date", "customer_request_date", "standard_delivery_date",
                     "confirmed_delivery_date", "actual_delivery_date")
        },
        "order_lines": {"numeric": ("quantity", "unit_price_recorded", "line_value")},
        "price_components": {"numeric": ("amount",), "date": ("posted_at",)},
        "products": {"numeric": ("shelf_life_days",)},
        "list_prices": {"numeric": ("list_price",), "date": ("valid_from",)},
        "product_costs": {"numeric": ("unit_cost",), "date": ("valid_from",)},
        "product_stages": {"date": ("valid_from", "valid_to")},
        "customers": {
            "numeric": ("credit_terms_days", "delivery_point_count"),
            "date": ("first_order_date",),
        },
        "customer_classes": {
            "numeric": ("price_adjustment_factor",),
            "date": ("valid_from", "valid_to"),
        },
        "customer_periods": {"date": ("valid_from", "valid_to")},
        "channels": {"numeric": ("typical_drop_size",)},
        "opportunities": {
            "numeric": ("requested_quantity", "line_item_count"),
            "date": ("created_at", "requested_delivery_date"),
        },
        "quotes": {
            "numeric": ("unit_price", "quantity", "round_number", "quoted_lead_time_days",
                        "model_suggested_price", "test_offset"),
            "date": ("issued_at",),
        },
        "deal_outcomes": {"date": ("decided_at",)},
        "agents": {"date": ("hire_date",)},
    },
    build_facts=_axiom_extract,
    uncertified_reasons=_axiom_reasons,
    quality_flags=_axiom_flags,
    validate_source=_axiom_validate,
    currency_column="currency_code",
    date_column="order_date",
    quantity_column="quantity",
    unit_price_column="unit_price_recorded",
    default_source="graph_json",
    description="AXIOM knowledge-graph PoC: orders, order lines and posted price components.",
)


# ---------------------------------------------------------------------------
# axiom_v2
# ---------------------------------------------------------------------------

#: The v2 export uses the PoC's labels but means two things differently, and
#: both are declared here rather than detected, because a rule found in the
#: data is a pattern, and a pattern can stop holding without anyone noticing.
#:
#: **What `line_value` already contains.** In v2 the line value is the list
#: price after the line's invoice discount and promotion allowance (checked on
#: every read by `_axiom_validate`). Those two are therefore the
#: list-to-line drop, `negotiated_off_list`, and are kept out of the bands.
#: Freight is not inside it, so it stays in the on-invoice band.
#:
#: **Where off-invoice money is posted.** Payment discounts are posted against
#: the whole order. Following the dataset's own "two frames", the off-invoice
#: band is therefore an order-grain step: per product the waterfall runs from
#: list value to the line value and COGS; per order it runs all the way to pocket
#: margin. Nothing posted against an order is split across its products.
_AXIOM_V2_IN_LINE_VALUE = ("invoice_discount", "promotion_allowance")

#: The currency every v2 amount is reported in.
_AXIOM_V2_CURRENCY = "SAR"

_AXIOM_V2_STEPS = propagate_grain(
    (
        Step("list_value", Kind.LEVEL, Origin.MEASURE, LINE, "List value",
             "PriceComponent step_name='list_price' at line grain"),
        Step("negotiated_off_list", Kind.DEDUCTION, Origin.DERIVED, LINE,
             "- line discounts and promotions",
             "invoice_discount + promotion_allowance, already netted into line_value"),
        Step("gross_value", Kind.LEVEL, Origin.MEASURE, LINE, "= Line value",
             "OrderLine.line_value"),
        Step("on_invoice_discount", Kind.DEDUCTION, Origin.MEASURE, KEY,
             "- on-invoice freight",
             "PriceComponent on_invoice=true not inside line_value: freight, at line "
             "and order grain"),
        Step("invoice_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Invoice price"),
        Step("off_invoice_leakage", Kind.DEDUCTION, Origin.MEASURE, KEY,
             "- off-invoice leakage",
             "PriceComponent on_invoice=false: returns_credit at line grain and "
             "payment_discount at order grain, summed per order"),
        Step("pocket_price", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket price"),
        Step("cogs", Kind.DEDUCTION, Origin.MEASURE, LINE, "- COGS",
             "quantity x ProductCostPeriod.unit_cost in force on the order date"),
        Step("pocket_margin", Kind.LEVEL, Origin.DERIVED, KEY, "= Pocket margin"),
    )
)


#: Win/loss history, as declared for this graph rather than inferred from it.
#:
#: **What counts as a decision.** A won deal is a win; a deal lost to a
#: competitor, or withdrawn by the customer after quoting, is a loss -- either
#: way the quote did not become an order. A deal we chose not to bid on has no
#: quote to learn from, and an open one has no outcome yet.
#:
#: **Which offer was decided.** The winning quote for a win; the last round
#: quoted for a loss.
#:
#: **Where the price position is recorded.** The graph's price tests are
#: "offset applied to model baseline", and a model-suggested quote *is* the
#: baseline. So for those two origins `test_offset` is the price position,
#: exactly. For list, contract and negotiated quotes `test_offset` is also 0,
#: but it means "no test", not "at the baseline" -- their position is unknown.
_AXIOM_OUTCOMES = {"won": 1, "lost_to_competitor": 0, "customer_withdrew": 0}
_AXIOM_PRICED_ORIGINS = ("model_suggested", "random_test")
_AXIOM_WIN_NUMERIC = (
    "round_number", "is_tender", "lowest_price_mandated", "incumbent_known",
    "competitors_known", "lead_time_gap_days", "log_quantity",
)
_AXIOM_WIN_CATEGORICAL = ("customer_type", "account_class", "channel_type", "currency_code")


def _valid_periods(periods: pd.DataFrame) -> pd.DataFrame:
    """Drop validity periods that end on or before they start: they cover no day."""
    if "valid_to" not in periods:
        return periods
    return periods[periods["valid_to"].isna() | (periods["valid_to"] > periods["valid_from"])]


def _in_force(
    periods: pd.DataFrame, facts: pd.DataFrame, key: str, date_column: str
) -> pd.DataFrame:
    """Each fact joined to the period in force on its date, half-open [from, to)."""
    periods = _valid_periods(periods)
    joined = facts[[key, date_column]].drop_duplicates().merge(periods, on=key, how="left")
    keep = joined["valid_from"].isna() | (joined["valid_from"] <= joined[date_column])
    if "valid_to" in joined:
        keep &= joined["valid_to"].isna() | (joined[date_column] < joined["valid_to"])
    return (
        joined[keep]
        .sort_values("valid_from")
        .groupby([key, date_column], as_index=False, dropna=False)
        .last()
        .drop(columns=[c for c in ("valid_from", "valid_to") if c in joined])
    )


def _axiom_win_data(src: Source):
    """One row per decided offer, with only what was known when it was quoted."""
    from .models.win_probability import WinData

    quotes = src.table("quotes")
    outcomes = src.table("deal_outcomes")
    opps = src.table("opportunities")
    excluded: dict[str, int] = {}

    result = outcomes["result"].astype(str)
    for label, n in result[~result.isin(_AXIOM_OUTCOMES)].value_counts().items():
        reason = {
            "no_bid": "deals we chose not to bid on (no quote to learn from)",
            "open": "deals still open (no outcome yet)",
        }.get(label, f"deals with outcome '{label}'")
        excluded[reason] = int(n)
    decided = outcomes[result.isin(_AXIOM_OUTCOMES)].copy()
    decided["won"] = decided["result"].astype(str).map(_AXIOM_OUTCOMES)

    # A re-bid replaces the deal it revises; counting both would count the same
    # customer need twice. The latest deal in each chain is the one kept.
    revised = set(opps["revises_opportunity_id"].dropna().astype(str))
    before = len(decided)
    decided = decided[~decided["opportunity_id"].astype(str).isin(revised)]
    if before - len(decided):
        excluded["earlier versions of deals that were re-bid"] = before - len(decided)

    last_round = (
        quotes.sort_values(["round_number", "issued_at"])
        .groupby("opportunity_id")
        .tail(1)[["opportunity_id", "quote_id"]]
    )
    decided = decided.merge(last_round, on="opportunity_id", how="left")
    won = decided["won"] == 1
    decided.loc[won, "quote_id"] = decided.loc[won, "winning_quote_id"]
    missing = int(decided["quote_id"].isna().sum())
    if missing:
        excluded["decided deals with no quote on record"] = missing
    decided = decided.dropna(subset=["quote_id"])

    q = quotes.drop(columns=["opportunity_id"]).merge(
        decided[["opportunity_id", "quote_id", "won", "decided_at"]], on="quote_id"
    )
    q = q.merge(
        opps[["opportunity_id", "customer_id", "channel_id", "is_tender",
              "lowest_price_mandated", "requested_delivery_date"]],
        on="opportunity_id", how="left",
    )
    q = q.merge(
        src.table("customers")[["customer_id", "customer_type"]], on="customer_id", how="left"
    ).merge(
        src.table("channels")[["channel_id", "type"]].rename(columns={"type": "channel_type"}),
        on="channel_id", how="left",
    )
    classes = _in_force(
        src.table("customer_classes")[["customer_id", "valid_from", "valid_to", "account_class"]],
        q, "customer_id", "issued_at",
    )
    q = q.merge(classes, on=["customer_id", "issued_at"], how="left")

    # Competitors as known on the day the quote went out -- a competitor learned about later
    # could not have shaped the price, and using it would flatter the model on history.
    comp = src.table("competitors")[["opportunity_id", "learned_at", "is_incumbent"]]
    known = q[["quote_id", "opportunity_id", "issued_at"]].merge(comp, on="opportunity_id")
    known = known[known["learned_at"] <= known["issued_at"]]
    per_quote = known.groupby("quote_id").agg(
        competitors_known=("opportunity_id", "size"),
        incumbent_known=("is_incumbent", lambda s: float(_flag(s).any())),
    )
    q = q.merge(per_quote, on="quote_id", how="left")
    q[["competitors_known", "incumbent_known"]] = q[
        ["competitors_known", "incumbent_known"]
    ].fillna(0.0)

    q["is_tender"] = _flag(q["is_tender"]).astype(float)
    q["lowest_price_mandated"] = _flag(q["lowest_price_mandated"]).astype(float)
    asked_days = (q["requested_delivery_date"] - q["issued_at"]).dt.days
    q["lead_time_gap_days"] = q["quoted_lead_time_days"] - asked_days
    q["log_quantity"] = np.log1p(q["quantity"].clip(lower=0))
    q["account_class"] = q["account_class"].fillna("unknown")

    origin = q["price_origin"].astype(str)
    q["price_position"] = np.where(
        origin.isin(_AXIOM_PRICED_ORIGINS), q["test_offset"].astype(float), np.nan
    )
    unpriced = int(q["price_position"].isna().sum())
    if unpriced:
        excluded["list, contract or negotiated quotes (price position not recorded)"] = unpriced

    frame = q.rename(columns={"opportunity_id": "deal_id"})[
        ["deal_id", "quote_id", "customer_id", "decided_at", "issued_at", "won",
         "price_position", "price_origin", *_AXIOM_WIN_NUMERIC, *_AXIOM_WIN_CATEGORICAL]
    ]
    return WinData(
        frame=frame.sort_values("decided_at", ignore_index=True),
        numeric=_AXIOM_WIN_NUMERIC,
        categorical=_AXIOM_WIN_CATEGORICAL,
        excluded=excluded,
        position_meaning="the offset from the pricing model's baseline price",
    )


def _axiom_fx(src: Source) -> pd.DataFrame:
    """Exchange rates as stated in the graph, both directions, half-open periods."""
    fx = src.table("exchange_rates")
    return _valid_periods(
        fx[["from_currency", "to_currency", "valid_from", "valid_to", "rate"]].copy()
    )


def _axiom_reference_prices(src: Source):
    """Per product: today's list price and cost, and the baseline price, observed."""
    from .models.win_probability import ReferencePrices

    fx = _axiom_fx(src)
    list_prices = _valid_periods(src.table("list_prices"))
    costs = _valid_periods(src.table("product_costs"))
    currency = str(list_prices["currency_code"].dropna().mode().iloc[0])

    win = _axiom_win_data(src).frame
    observed = win[(win["won"] == 1) & win["price_position"].notna()]
    orders = src.table("orders")[["order_id", "opportunity_id"]]
    lines = src.table("order_lines")[["order_id", "product_id"]]
    single = lines.groupby("order_id").filter(lambda g: g["product_id"].nunique() == 1)
    single = single.drop_duplicates("order_id")

    quotes = src.table("quotes")[["quote_id", "unit_price", "currency_code", "issued_at"]]
    obs = (
        observed[["deal_id", "quote_id", "price_position"]]
        .merge(quotes, on="quote_id")
        .merge(orders, left_on="deal_id", right_on="opportunity_id")
        .merge(single, on="order_id")
    )
    refs = ReferencePrices(products=pd.DataFrame(), currency=currency, fx=fx)
    obs["baseline"] = obs["unit_price"] / (1.0 + obs["price_position"])
    obs["baseline"] = [
        refs.to_reference_currency(b, c, d)
        for b, c, d in zip(obs["baseline"], obs["currency_code"], obs["issued_at"], strict=True)
    ]
    in_force = _in_force(
        list_prices[["product_id", "valid_from", "valid_to", "list_price"]],
        obs, "product_id", "issued_at",
    )
    obs = obs.merge(in_force, on=["product_id", "issued_at"], how="left")
    obs["ratio"] = obs["baseline"] / obs["list_price"]
    obs = obs[np.isfinite(obs["ratio"])]

    per_product = obs.groupby("product_id").agg(
        reference_ratio=("ratio", "median"),
        n_reference_observations=("ratio", "size"),
        reference_ids=("quote_id", lambda s: ";".join(sorted(map(str, s)))),
    )

    def latest(periods: pd.DataFrame, column: str) -> pd.Series:
        current = periods[periods["valid_to"].isna()] if "valid_to" in periods else periods
        current = current if len(current) else periods
        return current.sort_values("valid_from").groupby("product_id")[column].last()

    products = pd.DataFrame(
        {"list_price": latest(list_prices, "list_price"), "unit_cost": latest(costs, "unit_cost")}
    ).join(per_product, how="left")
    products["reference_price"] = products["reference_ratio"] * products["list_price"]
    products["n_reference_observations"] = (
        products["n_reference_observations"].fillna(0).astype(int)
    )
    return ReferencePrices(
        products=products.reset_index(names="product_id"),
        currency=currency,
        fx=fx,
        method=(
            "baseline price observed on won model-suggested and price-test quotes for "
            "single-product orders, as a share of the list price in force that day; "
            "the median share is applied to today's list price"
        ),
    )


def _axiom_v2_extract(src: Source) -> Extract:
    """The PoC extract, with v2's line composition and order-grain off-invoice band."""
    return _axiom_extract(
        src,
        in_line_value=_AXIOM_V2_IN_LINE_VALUE,
        key_bands=("on_invoice_discount", "off_invoice_leakage"),
        currency=_AXIOM_V2_CURRENCY,
    )


def _axiom_v2_validate(src: Source, report):
    """The PoC checks, with v2's declared composition verified line by line."""
    return _axiom_validate(
        src, report, in_line_value=_AXIOM_V2_IN_LINE_VALUE, off_invoice_grain=KEY,
        currency=_AXIOM_V2_CURRENCY,
    )


AXIOM_V2 = replace(
    AXIOM_POC,
    name="axiom_v2",
    steps=_AXIOM_V2_STEPS,
    tables={
        **AXIOM_POC.tables,
        "competitors": "CompetitorPresence",
        "exchange_rates": "ExchangeRatePeriod",
    },
    semantic_types={
        **AXIOM_POC.semantic_types,
        "list_prices": {"numeric": ("list_price",), "date": ("valid_from", "valid_to")},
        "product_costs": {"numeric": ("unit_cost",), "date": ("valid_from", "valid_to")},
        "competitors": {"numeric": ("their_price",), "date": ("learned_at",)},
        "exchange_rates": {"numeric": ("rate",), "date": ("valid_from", "valid_to")},
    },
    build_facts=_axiom_v2_extract,
    validate_source=_axiom_v2_validate,
    win_data=_axiom_win_data,
    reference_prices=_axiom_reference_prices,
    description="AXIOM pricing data v2: line amounts already include their discounts; "
    "early-payment discounts are recorded per order.",
)


REGISTRY: dict[str, Profile] = {
    B2B_INDUSTRIAL.name: B2B_INDUSTRIAL,
    DAIRY_AL_KHARJ.name: DAIRY_AL_KHARJ,
    AXIOM_POC.name: AXIOM_POC,
    AXIOM_V2.name: AXIOM_V2,
}


def get_profile(name: str | None = None) -> Profile:
    """The profile named by PRICING_PROFILE, or the one asked for."""
    from .config import PROFILE

    key = (name or PROFILE).strip()
    if key not in REGISTRY:
        raise ValueError(f"Unknown pricing profile {key!r}. Known: {sorted(REGISTRY)}")
    return REGISTRY[key]


def register(profile: Profile) -> Profile:
    """Add a host project's own profile to the registry."""
    REGISTRY[profile.name] = profile
    return profile
