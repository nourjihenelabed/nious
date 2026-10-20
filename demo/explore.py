"""Business views over one engine run: filter orders, break them down, size the prize."""

from __future__ import annotations

import numpy as np
import pandas as pd

STEPS = (
    "list_value", "negotiated_off_list", "gross_value", "on_invoice_discount",
    "invoice_price", "off_invoice_leakage", "pocket_price", "cogs", "pocket_margin",
)

#: Plain names for the segment labels the engine derives from behaviour. The
#: engine's own label is kept beside it, so nothing is hidden.
SEGMENT_NAMES = {
    "price-taking": ("Loyal payers", "Pay close to list price and rarely negotiate. Your best-margin customers."),
    "negotiated but profitable": ("Smart negotiators", "Negotiate, but still leave a healthy margin. The core of the book."),
    "volume-driven, heavily negotiated": ("Big-volume hagglers", "Buy a lot and push hard on price. Big revenue, thinner margin."),
    "premium anchor": ("Premium anchors", "Large customers who pay near list. Protect them."),
    "small and heavily discounted": ("Small, heavily discounted", "Small accounts that get large discounts. Review their terms."),
    "thin margin, little discounting -- check cost to serve": ("Thin margin, low discount", "Price is not the problem here: check cost to serve and product mix."),
    "unsegmented": ("Too new to tell", "Too few orders to read a buying habit yet."),
}

#: ISIC rev. 4 divisions that appear in the data, in words.
INDUSTRY_NAMES = {
    "ISIC10": "Food manufacturing", "ISIC11": "Beverages", "ISIC20": "Chemicals", "ISIC22": "Rubber & plastics",
    "ISIC25": "Metal products", "ISIC28": "Machinery", "ISIC41": "Construction", "ISIC46": "Wholesale trade",
    "ISIC47": "Retail trade", "ISIC52": "Warehousing & logistics", "ISIC55": "Hotels", "ISIC56": "Restaurants & catering",
    "ISIC84": "Public administration", "ISIC85": "Education", "ISIC86": "Healthcare",
}

#: The filters the page offers, and the order column each reads.
FILTERS = {
    "channel": "channel",
    "region": "region",
    "customer_type": "customer_type",
    "segment": "segment",
    "account_class": "account_class",
    "industry": "industry",
    "customer": "customer_id",
}

DIMENSIONS = {
    "channel": "Sales channel",
    "region": "Region",
    "customer_type": "Customer type",
    "segment": "Buying behaviour",
    "account_class": "Account class",
    "industry": "Industry",
}


def segment_name(label: str) -> str:
    return SEGMENT_NAMES.get(label, (label, ""))[0]


class Explorer:
    """Orders and lines of one run, ready to filter."""

    def __init__(self, run):
        w = run.waterfall
        seg = run.models.get("segmentation")
        labels = seg.label_of() if seg is not None else {}
        channel_names = (
            w.facts[["channel_id", "channel_type"]].drop_duplicates().set_index("channel_id")["channel_type"].to_dict()
            if "channel_type" in w.facts.columns else {}
        )

        orders = w.keys[w.keys["certified"]].copy()
        orders["channel"] = orders["channel_id"].map(channel_names).fillna(orders["channel_id"]).str.title()
        orders["segment_label"] = orders["customer_id"].astype(str).map(labels).fillna("unsegmented")
        orders["segment"] = orders["segment_label"].map(segment_name)
        orders["customer_type"] = orders["customer_type"].astype(str).str.title()
        orders["account_class"] = orders["account_class"].astype(str).str.title()
        orders["industry"] = orders["industry_code"].map(INDUSTRY_NAMES).fillna(orders["industry_code"])
        orders["month"] = orders["period"].astype(str)
        self.orders = orders

        lines = w.facts[w.facts["certified"]].copy()
        lines = lines.merge(orders[["order_id", *[c for c in FILTERS.values() if c != "customer_id"], "month"]],
                            on="order_id", how="inner", suffixes=("_line", ""))
        lines["line_margin"] = lines["gross_value"] - lines["cogs"]
        lines["family_name"] = lines["family"].astype(str).str.replace("FAM0", "Family ", regex=False)
        self.lines = lines

        self.currency = orders["currency_code"].mode().iloc[0] if "currency_code" in orders else ""
        self.months = sorted(orders["month"].unique())
        self.run = run
        self.labels = labels
        names = (
            w.facts.drop_duplicates("customer_id").set_index("customer_id")["legal_name"]
            if "legal_name" in w.facts.columns else pd.Series(dtype=str)
        )
        # Some names read "Company — City"; show them as "Company, City".
        self.customer_names = names.astype(str).str.replace(" — ", ", ", regex=False).to_dict()
        self.recs = run.models.get("recommendations")
        self._opportunity = None

    # ------------------------------------------------------------------ filters
    def options(self) -> dict:
        o = self.orders
        return {
            "filters": {k: sorted(o[c].dropna().astype(str).unique().tolist()) for k, c in FILTERS.items() if k != "customer"},
            "months": self.months,
            "dimensions": DIMENSIONS,
            "currency": self.currency,
            "segments": {
                segment_name(lbl): {"label": lbl, "story": SEGMENT_NAMES.get(lbl, (lbl, ""))[1]}
                for lbl in sorted(set(self.labels.values()) | {"unsegmented"})
            },
        }

    def filtered(self, f: dict) -> pd.DataFrame:
        o = self.orders
        mask = np.ones(len(o), dtype=bool)
        for key, col in FILTERS.items():
            values = f.get(key)
            if values:
                mask &= o[col].astype(str).isin(values)
        if f.get("from"):
            mask &= o["month"] >= f["from"]
        if f.get("to"):
            mask &= o["month"] <= f["to"]
        return o[mask]

    # ------------------------------------------------------------------ views
    @staticmethod
    def _summary(df: pd.DataFrame) -> dict:
        s = {k: float(df[k].sum()) for k in STEPS}
        lv = s["list_value"] or np.nan
        s.update(
            orders=int(len(df)),
            customers=int(df["customer_id"].nunique()),
            quantity=float(df["quantity"].sum()),
            margin_pct=100 * s["pocket_margin"] / s["pocket_price"] if s["pocket_price"] else None,
            kept_pct=100 * s["pocket_price"] / lv if lv == lv else None,
            given_away=s["list_value"] - s["pocket_price"],
            given_away_pct=100 * (s["list_value"] - s["pocket_price"]) / lv if lv == lv else None,
        )
        return s

    def _breakdown(self, df: pd.DataFrame, col: str) -> list[dict]:
        rows = []
        for key, g in df.groupby(col):
            r = self._summary(g)
            r["key"] = str(key)
            rows.append(r)
        return sorted(rows, key=lambda r: -r["pocket_price"])

    def view(self, f: dict) -> dict:
        df = self.filtered(f)
        base = self._summary(self.orders)
        out = {
            "summary": self._summary(df),
            "book": base,
            "breakdowns": {k: self._breakdown(df, k) for k in DIMENSIONS},
            "trend": self._breakdown(df, "month"),
            "heatmap": self._heatmap(df),
            "customers": self._customers(df),
            "products": self._products(df),
        }
        out["trend"].sort(key=lambda r: r["key"])
        out["insights"] = self._insights(df, out)
        return out

    def _heatmap(self, df: pd.DataFrame) -> dict:
        cells = []
        for (ch, rg), g in df.groupby(["channel", "region"]):
            s = self._summary(g)
            cells.append({"channel": ch, "region": rg, "margin_pct": s["margin_pct"], "revenue": s["pocket_price"], "orders": s["orders"]})
        return {"channels": sorted(df["channel"].unique().tolist()), "regions": sorted(df["region"].unique().tolist()), "cells": cells}

    def _customers(self, df: pd.DataFrame) -> list[dict]:
        rows = []
        for cid, g in df.groupby("customer_id"):
            s = self._summary(g)
            rows.append({
                "customer_id": cid,
                "name": self.customer_names.get(cid, ""),
                "segment": g["segment"].iloc[0],
                "customer_type": g["customer_type"].iloc[0],
                "channel": g["channel"].mode().iloc[0],
                "region": g["region"].mode().iloc[0],
                "orders": s["orders"],
                "revenue": s["pocket_price"],
                "margin": s["pocket_margin"],
                "margin_pct": s["margin_pct"],
                "given_away_pct": s["given_away_pct"],
            })
        return sorted(rows, key=lambda r: -r["revenue"])

    def _products(self, df: pd.DataFrame) -> dict:
        lines = self.lines[self.lines["order_id"].isin(df["order_id"])]

        def group(col: str, name_col: str | None = None) -> list[dict]:
            rows = []
            for key, g in lines.groupby(col):
                gross, lst, cogs = g["gross_value"].sum(), g["list_value"].sum(), g["cogs"].sum()
                rows.append({
                    "key": str(key),
                    "name": str(g[name_col].iloc[0]) if name_col else str(key),
                    "revenue": float(gross),
                    "list_value": float(lst),
                    "margin": float(gross - cogs),
                    "margin_pct": float(100 * (gross - cogs) / gross) if gross else None,
                    "negotiated_pct": float(100 * (lst - gross) / lst) if lst else None,
                    "quantity": float(g["quantity"].sum()),
                    "customers": int(g["customer_id"].nunique()),
                })
            return sorted(rows, key=lambda r: -r["revenue"])

        return {"families": group("family_name"), "products": group("product_id", "product_name")}

    def _insights(self, df: pd.DataFrame, view: dict) -> list[dict]:
        """A handful of plain sentences a sales director would say out loud."""
        s = view["summary"]
        cur = self.currency
        out = []
        if s["list_value"]:
            leaks = [("negotiated off the list price", s["negotiated_off_list"]),
                     ("in discounts on the invoice", s["on_invoice_discount"]),
                     ("in rebates and allowances after the invoice", s["off_invoice_leakage"])]
            name, amount = max(leaks, key=lambda x: x[1])
            out.append({"tone": "warn", "title": "The biggest leak",
                        "text": f"{_money(amount, cur)} goes {name}: {100 * amount / s['list_value']:.1f}% of list value."})
        for dim, title in (("channel", "channel"), ("customer_type", "customer type"), ("region", "region")):
            rows = [r for r in view["breakdowns"][dim] if r["orders"] >= 20 and r["margin_pct"] is not None]
            if len(rows) >= 2:
                lo, hi = min(rows, key=lambda r: r["margin_pct"]), max(rows, key=lambda r: r["margin_pct"])
                gap = hi["margin_pct"] - lo["margin_pct"]
                if gap >= 2:
                    out.append({"tone": "info", "title": f"Margin gap by {title}",
                                "text": f"{hi['key']} keeps {hi['margin_pct']:.1f}% margin, {lo['key']} only {lo['margin_pct']:.1f}%: a {gap:.1f}-point gap.",
                                "filter": {dim: [lo["key"]]}})
                    break
        cust = view["customers"]
        if len(cust) >= 10:
            top = cust[: max(1, len(cust) // 10)]
            share = sum(c["revenue"] for c in top) / max(1e-9, sum(c["revenue"] for c in cust))
            out.append({"tone": "info", "title": "Concentration",
                        "text": f"The top 10% of customers ({len(top)}) bring {100 * share:.0f}% of pocket revenue."})
        trend = view["trend"]
        if len(trend) >= 6:
            first = self._summary(df[df["month"].isin([t["key"] for t in trend[:3]])])
            last = self._summary(df[df["month"].isin([t["key"] for t in trend[-3:]])])
            if first["margin_pct"] and last["margin_pct"]:
                d = last["margin_pct"] - first["margin_pct"]
                out.append({"tone": "good" if d >= 0 else "warn", "title": "Margin trend",
                            "text": f"Margin moved from {first['margin_pct']:.1f}% in the first three months to {last['margin_pct']:.1f}% in the last three ({d:+.1f} points)."})
        opp = self.opportunity()
        if opp["total_extra_margin"]:
            out.append({"tone": "good", "title": "Money on the table",
                        "text": f"Recommended prices on the {opp['pairs_priced']} most-traded customer-product pairs would add about {_money(opp['total_extra_margin'], cur)} margin a year, after the volume each change is expected to cost or win back.",
                        "tab": "opportunity"})
        return out

    # ------------------------------------------------------------------ customer
    def customer(self, cid: str) -> dict:
        o = self.orders[self.orders["customer_id"].astype(str) == cid]
        if o.empty:
            return {}
        lines = self.lines[self.lines["customer_id"].astype(str) == cid]
        products = []
        for pid, g in lines.groupby("product_id"):
            gross, cogs = g["gross_value"].sum(), g["cogs"].sum()
            products.append({"product_id": pid, "name": g["product_name"].iloc[0], "revenue": float(gross),
                             "quantity": float(g["quantity"].sum()), "margin_pct": float(100 * (gross - cogs) / gross) if gross else None,
                             "avg_price": float(g["unit_price_recorded"].median()) if "unit_price_recorded" in g else None})
        products.sort(key=lambda r: -r["revenue"])
        recs = []
        if self.recs is not None:
            for _, r in self.recs[self.recs["customer_id"].astype(str) == cid].iterrows():
                recs.append({k: r[k] for k in ("product_id", "status", "low", "mid", "high", "current_price", "uplift_pct", "acceptance", "note")})
        label = o["segment_label"].iloc[0]
        win = self.run.models.get("win_data")
        quotes = None
        if win is not None:
            q = win.frame[win.frame["customer_id"].astype(str) == cid]
            quotes = {"quotes": int(len(q)), "won": int(q["won"].sum()) if len(q) else 0}
        return {
            "customer_id": cid,
            "name": self.customer_names.get(cid, ""),
            "segment": segment_name(label),
            "segment_label": label,
            "segment_story": SEGMENT_NAMES.get(label, (label, ""))[1],
            "customer_type": o["customer_type"].iloc[0],
            "industry": o["industry"].iloc[0],
            "region": o["region"].iloc[0],
            "city": o["city"].iloc[0] if "city" in o else "",
            "channels": sorted(o["channel"].unique().tolist()),
            "summary": self._summary(o),
            "trend": sorted(self._breakdown(o, "month"), key=lambda r: r["key"]),
            "products": products,
            "recommendations": recs,
            "quotes": quotes,
        }

    # ------------------------------------------------------------------ opportunity
    def opportunity(self) -> dict:
        """What the recommended prices are worth, on last year's volume."""
        if self._opportunity is not None:
            return self._opportunity
        recs = self.recs
        empty = {"total_extra_margin": 0.0, "pairs_priced": 0, "rows": [], "by_segment": [], "by_channel": [], "window": ""}
        if recs is None or not len(recs):
            self._opportunity = empty
            return empty
        last = self.months[-12:] if len(self.months) >= 12 else self.months
        recent = self.lines[self.lines["month"].isin(last)]
        qty = recent.groupby(["customer_id", "product_id"])["quantity"].sum()
        ch = self.orders.groupby("customer_id")["channel"].agg(lambda s: s.mode().iloc[0])
        rows = []
        for _, r in recs[recs["status"] == "complete"].iterrows():
            q = float(qty.get((r["customer_id"], r["product_id"]), 0.0))
            if not q or pd.isna(r["current_price"]) or pd.isna(r["unit_cost"]):
                continue
            share = r.get("volume_share_at_mid")
            share = 1.0 if share is None or pd.isna(share) else float(share)
            new_q = q * share
            extra = new_q * (r["mid"] - r["unit_cost"]) - q * (r["current_price"] - r["unit_cost"])
            label = self.labels.get(str(r["customer_id"]), "unsegmented")
            rows.append({
                "customer_id": r["customer_id"], "name": self.customer_names.get(r["customer_id"], ""),
                "product_id": r["product_id"], "segment": segment_name(label), "channel": ch.get(r["customer_id"], ""),
                "today": float(r["current_price"]), "recommended": float(r["mid"]), "low": float(r["low"]), "high": float(r["high"]),
                "change_pct": float(r["uplift_pct"]) if pd.notna(r["uplift_pct"]) else None,
                "volume_now": q, "volume_after": new_q, "extra_margin": float(extra),
                "win_chance": float(r["acceptance"]) if pd.notna(r["acceptance"]) else None,
            })
        rows.sort(key=lambda x: -x["extra_margin"])

        def by(col):
            agg = {}
            for x in rows:
                a = agg.setdefault(x[col], {"key": x[col], "extra_margin": 0.0, "pairs": 0})
                a["extra_margin"] += x["extra_margin"]
                a["pairs"] += 1
            return sorted(agg.values(), key=lambda a: -a["extra_margin"])

        self._opportunity = {
            "total_extra_margin": float(sum(x["extra_margin"] for x in rows)),
            "raise": sum(1 for x in rows if (x["change_pct"] or 0) > 0),
            "lower": sum(1 for x in rows if (x["change_pct"] or 0) < 0),
            "pairs_priced": len(rows),
            "pairs_considered": int(len(recs)),
            "rows": rows,
            "by_segment": by("segment"),
            "by_channel": by("channel"),
            "window": f"{last[0]} to {last[-1]}",
        }
        return self._opportunity


def _money(x: float, cur: str) -> str:
    a = abs(x)
    if a >= 1e6:
        return f"{x / 1e6:,.1f} M {cur}"
    if a >= 1e3:
        return f"{x / 1e3:,.0f} k {cur}"
    return f"{x:,.0f} {cur}"
