"""Build the two comparison datasets, both derived from the delivered graph.

    python scripts/make_scenario.py reduced
    python scripts/make_scenario.py topup
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_SOURCE = REPO_ROOT.parent / "axiom_pricing_module_Knowledge-graph_PoC.json"
OUT_DIR = REPO_ROOT / "data" / "scenarios"


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------

def _node(session: str, counter: list[int], label: str, properties: dict) -> dict:
    """One node in export shape, with an id that looks like the real ones."""
    counter[0] += 1
    return {
        "id": f"4:{session}:{counter[0]}",
        "labels": [label],
        "properties": properties,
    }


def _edge(source: str, edge_type: str, target: str) -> dict:
    """One relationship in export shape."""
    return {"source": source, "type": edge_type, "target": target, "properties": {}}


def _document(session: str, label: str, nodes: list[dict], edges: list[dict]) -> dict:
    """Wrap nodes and edges as an export, with the self-stated counts."""
    return {
        "axiom_graph_export": 1,
        "session_id": session,
        "session_label": label,
        "exported_at": f"{date.today().isoformat()}T00:00:00Z",
        "includes_internal_properties": False,
        "nodes": nodes,
        "relationships": edges,
        "summary": {
            "complete": True,
            "node_count": len(nodes),
            "relationship_count": len(edges),
        },
    }


def _write(doc: dict, out: Path) -> Path:
    """Write the export and report what is in it."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    labels = Counter(n["labels"][0] for n in doc["nodes"])
    print(f"\nWrote {out}")
    print(f"  {len(doc['nodes']):,} nodes, {len(doc['relationships']):,} relationships")
    for label, n in sorted(labels.items(), key=lambda kv: -kv[1]):
        print(f"    {n:6,}  {label}")
    print("\nRun it with:")
    print("  export PRICING_PROFILE=axiom_poc")
    print(f"  export PRICING_GRAPH_JSON={out.relative_to(REPO_ROOT)}")
    print(f"  export PRICING_REPORTS_DIR=reports_{out.stem}")
    return out


# --------------------------------------------------------------------------
# reduced: the delivered graph, cut down
# --------------------------------------------------------------------------

#: Labels that are reference data rather than transactions.
REFERENCE_LABELS = {
    "SellableProduct", "TradeCustomer", "Channel", "SalesAgent", "City",
    "ProductFamily", "ProductLine", "RivalSeller", "ListPricePeriod",
    "ProductCostPeriod", "ProductStagePeriod", "ProductSubstitute",
    "CustomerClassPeriod", "AuthorityPeriod", "IncentivePeriod",
}


def reduce_export(doc: dict, n_orders: int, drop_cost_periods: float, seed: int) -> dict:
    """Keep `n_orders` orders and everything that belongs to them."""
    rng = random.Random(seed)
    by_label: dict[str, list[dict]] = defaultdict(list)
    for node in doc["nodes"]:
        by_label[node["labels"][0]].append(node)

    orders = sorted(by_label["Order"], key=lambda n: n["properties"]["order_id"])
    keep_orders = orders[:n_orders]
    order_ids = {n["properties"]["order_id"] for n in keep_orders}
    opportunity_ids = {n["properties"].get("opportunity_id") for n in keep_orders}

    lines = [n for n in by_label["OrderLine"] if n["properties"]["order_id"] in order_ids]
    line_ids = {n["properties"]["order_line_id"] for n in lines}

    # Customer-quarter periods are kept for the customers still in the book, so
    # the unattributable-rebate warning keeps firing on the reduced set too.
    customer_ids = {n["properties"]["customer_id"] for n in keep_orders}
    periods = [
        n for n in by_label["CustomerPeriod"]
        if n["properties"]["customer_id"] in customer_ids
    ]
    period_ids = {n["id"] for n in periods}
    # A customer-period component names its period through an edge rather than a
    # readable key, so the edge is what decides whether it survives.
    period_component_ids = {
        r["source"] for r in doc["relationships"]
        if r["type"] == "APPLIES_TO_CUSTOMER_PERIOD" and r["target"] in period_ids
    }

    def component_survives(node: dict) -> bool:
        """A price component belongs to the book when the thing it is posted against does."""
        level = node["properties"].get("applies_to_level")
        target = node["properties"].get("applies_to_id")
        if level == "line":
            return target in line_ids
        if level == "order":
            return target in order_ids
        if level == "customer_period":
            return node["id"] in period_component_ids
        return False

    components = [n for n in by_label["PriceComponent"] if component_survives(n)]

    # Opportunities, and everything hanging off them.
    unfulfilled = [
        n for n in by_label["Opportunity"]
        if n["properties"]["opportunity_id"] not in opportunity_ids
    ]
    rng.shuffle(unfulfilled)
    n_keep_unfulfilled = round(len(unfulfilled) * n_orders / max(len(orders), 1))
    opportunities = [
        n for n in by_label["Opportunity"]
        if n["properties"]["opportunity_id"] in opportunity_ids
    ] + unfulfilled[:n_keep_unfulfilled]
    opp_ids = {n["properties"]["opportunity_id"] for n in opportunities}

    def by_opportunity(label: str) -> list[dict]:
        return [n for n in by_label[label] if n["properties"].get("opportunity_id") in opp_ids]

    kept: list[dict] = []
    for label, nodes in by_label.items():
        if label in REFERENCE_LABELS:
            kept.extend(nodes)
    kept.extend(keep_orders)
    kept.extend(lines)
    kept.extend(components)
    kept.extend(periods)
    kept.extend(opportunities)
    for label in ("Quote", "DealOutcome", "StageChange", "CompetitorPresence"):
        kept.extend(by_opportunity(label))

    # The completeness lever, applied after the cut so the order count is exact.
    if drop_cost_periods > 0:
        costs = [n for n in kept if n["labels"][0] == "ProductCostPeriod"]
        rng.shuffle(costs)
        drop = {id(n) for n in costs[: round(len(costs) * drop_cost_periods)]}
        kept = [n for n in kept if id(n) not in drop]
        print(
            f"  dropped {len(drop)} of {len(costs)} ProductCostPeriod nodes "
            "-- lines using those products can no longer compute COGS"
        )

    keep_ids = {n["id"] for n in kept}
    edges = [
        r for r in doc["relationships"]
        if r["source"] in keep_ids and r["target"] in keep_ids
    ]
    session = doc.get("session_id") or str(uuid.uuid4())
    return _document(
        session,
        f"AXIOM PoC reduced to {len(keep_orders)} orders"
        + (f", {drop_cost_periods:.0%} of cost periods removed" if drop_cost_periods else ""),
        kept,
        edges,
    )




# --------------------------------------------------------------------------
# topup: the delivered graph, plus enough trade to clear every gate
# --------------------------------------------------------------------------

#: True price elasticity per product family, used to generate the added orders and never shown to
#: the engine.
TRUE_ELASTICITY = {
    "FAM01": -2.10, "FAM02": -1.70, "FAM03": -1.40,
    "FAM04": -1.10, "FAM05": -0.90, "FAM06": -0.60,
}

#: The price every quantity is expressed relative to. Purely a generator device:
#: it keeps the quantities in a sensible range whatever a product's list price is.
REFERENCE_PRICE = 200.0

#: How far off list a month's price level can be drawn.
DISCOUNT_RANGE = (0.0, 0.40)

#: Lognormal noise on each added line's quantity around what its price implies.
LINE_NOISE_SD = 0.25

#: Months of added history per product.
TOPUP_MONTHS = 20

#: Lines added per product-month.
LINES_PER_CELL = 2

#: Steepness of the win curve on added quotes: the coefficient on the gap between
#: the quote and the unobserved competitor price, as a fraction of list.
WIN_SLOPE = 9.0

#: Added lost enquiries per added won one, so the funnel keeps its shape.
LOST_RATIO = 1.5


def _next_id(doc: dict) -> tuple[str, list[int]]:
    """The export's session prefix, and a counter safely past every existing id."""
    session, highest = "", 0
    for node in doc["nodes"]:
        parts = str(node["id"]).rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            session = session or parts[0].split(":", 1)[-1]
            highest = max(highest, int(parts[1]))
    return session or str(uuid.uuid4()), [highest + 1000]


def _max_suffix(values, prefix: str) -> int:
    """The highest number used by ids like ORD00100, so the next one is ORD00101."""
    best = 0
    for v in values:
        text = str(v)
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            best = max(best, int(text[len(prefix):]))
    return best


def topup(doc: dict, seed: int, months: int, per_cell: int) -> tuple[dict, dict]:
    """Keep the delivered graph whole and trade more of it until the gates open."""
    rng = random.Random(seed)
    session, counter = _next_id(doc)
    nodes, edges = doc["nodes"], doc["relationships"]

    def node(label: str, props: dict) -> dict:
        n = _node(session, counter, label, props)
        nodes.append(n)
        return n

    by_label: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_label[n["labels"][0]].append(n)

    before_orders = len(by_label["Order"])
    before_lines = len(by_label["OrderLine"])

    # -- read the real catalogue, and use it as it is ----------------------
    list_price = {
        n["properties"]["product_id"]: float(n["properties"]["list_price"])
        for n in by_label["ListPricePeriod"]
        if n["properties"].get("list_price") is not None
    }
    unit_cost = {
        n["properties"]["product_id"]: float(n["properties"]["unit_cost"])
        for n in by_label["ProductCostPeriod"]
        if n["properties"].get("unit_cost") is not None
    }
    product_node = {n["properties"]["product_id"]: n for n in by_label["SellableProduct"]}
    family_of = {
        pid: n["properties"].get("family") for pid, n in product_node.items()
    }
    # A product can only be added to the book if the book can price it and cost it.
    tradeable = sorted(
        pid for pid in product_node
        if pid in list_price and pid in unit_cost and family_of.get(pid) in TRUE_ELASTICITY
    )

    customer_node = {n["properties"]["customer_id"]: n for n in by_label["TradeCustomer"]}
    customers = sorted(customer_node)
    channels = list(by_label["Channel"])
    agents = list(by_label["SalesAgent"])

    if not tradeable or not customers or not channels or not agents:
        raise SystemExit(
            "this export does not carry the catalogue, customers, channels and "
            "agents the top-up needs to trade with"
        )

    # -- continue the real id sequences ------------------------------------
    order_seq = _max_suffix((n["properties"]["order_id"] for n in by_label["Order"]), "ORD")
    line_seq = _max_suffix(
        (n["properties"]["order_line_id"] for n in by_label["OrderLine"]), "OL"
    )
    comp_seq = _max_suffix(
        (n["properties"]["component_id"] for n in by_label["PriceComponent"]), "PC"
    )
    opp_seq = _max_suffix(
        (n["properties"]["opportunity_id"] for n in by_label["Opportunity"]), "OPP"
    )
    quote_seq = _max_suffix((n["properties"]["quote_id"] for n in by_label["Quote"]), "Q")

    # -- when the added trade happened ------------------------------------- Anchored to the
    # real book's own window, so the top-up reads as more of the same history rather than as a
    # separate period bolted on the end.
    real_dates = [
        n["properties"].get("order_date") for n in by_label["Order"]
        if n["properties"].get("order_date")
    ]
    first = date.fromisoformat(min(real_dates)[:10]).replace(day=1) if real_dates else date(2025, 1, 1)
    window = [_add_months(first, i) for i in range(months)]

    # -- the panel: a price per product-month, and the demand it produces ---
    basket: dict[date, list[dict]] = defaultdict(list)
    truth_rows: list[dict] = []
    scale = {pid: rng.uniform(70, 160) for pid in tradeable}
    for pid in tradeable:
        beta = TRUE_ELASTICITY[family_of[pid]]
        for month in window:
            discount = rng.uniform(*DISCOUNT_RANGE)
            price = round(list_price[pid] * (1 - discount), 2)
            expected = scale[pid] * (price / REFERENCE_PRICE) ** beta
            for _ in range(per_cell):
                quantity = max(
                    1.0, float(round(expected * math.exp(rng.gauss(0, LINE_NOISE_SD))))
                )
                basket[month].append({
                    "product_id": pid, "quantity": quantity, "unit_price": price,
                    "discount": discount, "family": family_of[pid], "true_elasticity": beta,
                    "list_price": list_price[pid],
                })

    # -- pack the panel into orders, each with an enquiry behind it ---------
    def price_component(step, kind, on_invoice, amount, level, target, posted):
        """One PriceComponent, posted the way the real export posts them."""
        nonlocal comp_seq
        comp_seq += 1
        return node("PriceComponent", {
            "component_id": f"PC{comp_seq:07d}", "step_name": step, "step_type": kind,
            "on_invoice": on_invoice, "is_estimated": "false", "amount": round(amount, 2),
            "applies_to_level": level, "applies_to_id": target,
            "currency_code": "SAR", "posted_at": posted,
        })

    def enquiry(cust: str, created: date, n_items: int) -> dict:
        """An enquiry and its quotes, quoted against an unobserved competitor."""
        nonlocal opp_seq, quote_seq
        opp_seq += 1
        opp_id = f"OPP{opp_seq:05d}"
        channel = channels[rng.randrange(len(channels))]
        opp = node("Opportunity", {
            "opportunity_id": opp_id, "customer_id": cust,
            "channel_id": channel["properties"]["channel_id"],
            "created_at": created.isoformat(),
            "requested_delivery_date": (created + timedelta(days=21)).isoformat(),
            "requested_quantity": float(rng.randint(20, 400)),
            "line_item_count": n_items,
            "is_tender": "true" if rng.random() < 0.18 else "false",
            "lowest_price_mandated": "false",
            "description": "Request for supply.",
        })
        edges.append(_edge(opp["id"], "FOR_CUSTOMER", customer_node[cust]["id"]))
        edges.append(_edge(opp["id"], "SOLD_THROUGH", channel["id"]))

        anchor = tradeable[rng.randrange(len(tradeable))]
        rival = list_price[anchor] * (1 - rng.uniform(0.05, 0.30))
        winner = None
        for round_no in range(1, rng.randint(1, 3) + 1):
            quote_seq += 1
            price = round(list_price[anchor] * (1 - rng.uniform(*DISCOUNT_RANGE)), 2)
            p_win = 1 / (1 + math.exp(WIN_SLOPE * (price - rival) / list_price[anchor]))
            agent = agents[rng.randrange(len(agents))]
            q = node("Quote", {
                "quote_id": f"Q{quote_seq:06d}", "opportunity_id": opp_id,
                "round_number": round_no, "unit_price": price,
                "quantity": float(rng.randint(20, 400)),
                "issued_at": (created + timedelta(days=7 * round_no)).isoformat(),
                "currency_code": "SAR",
                "issued_by_agent_id": agent["properties"]["agent_id"],
                "quoted_lead_time_days": float(rng.choice([7, 14, 21, 30])),
                "price_origin": rng.choice(["list", "contract", "negotiated", "random_test"]),
                "quote_notes": f"Round {round_no} offer.", "test_offset": 0.0,
                "model_suggested_price": None,
            })
            edges.append(_edge(q["id"], "FOR_OPPORTUNITY", opp["id"]))
            edges.append(_edge(q["id"], "ISSUED_BY", agent["id"]))
            if winner is None and rng.random() < p_win:
                winner = q
        return {"node": opp, "id": opp_id, "created": created, "won": winner, "last": q}

    def close(opp: dict, won: bool) -> None:
        """Record how the enquiry ended."""
        result = "won" if won else rng.choice(
            ["lost_to_competitor", "customer_withdrew", "no_bid", "open"]
        )
        outcome = node("DealOutcome", {
            "opportunity_id": opp["id"], "result": result,
            "winning_quote_id": opp["won"]["properties"]["quote_id"] if won else None,
            "decided_at": (opp["created"] + timedelta(days=40)).isoformat(),
            "reason_code": "price" if result == "lost_to_competitor" else "other",
            "reason_text": f"Closed as {result}.",
        })
        edges.append(_edge(outcome["id"], "FOR_OPPORTUNITY", opp["node"]["id"]))

    added_orders = added_lines = 0
    for month, items in sorted(basket.items()):
        rng.shuffle(items)
        cursor = 0
        while cursor < len(items):
            chunk = items[cursor:cursor + min(rng.randint(1, 4), len(items) - cursor)]
            cursor += len(chunk)

            cust = customers[rng.randrange(len(customers))]
            order_date = month + timedelta(days=rng.randrange(0, 27))
            opp = enquiry(cust, order_date - timedelta(days=45), len(chunk))
            # This enquiry produced an order, so it was won whatever the quoting
            # simulation rolled. The quotes stand; the last one is the winner.
            opp["won"] = opp["won"] or opp["last"]
            close(opp, won=True)

            order_seq += 1
            order_id = f"ORD{order_seq:05d}"
            order = node("Order", {
                "order_id": order_id, "opportunity_id": opp["id"], "customer_id": cust,
                "order_date": order_date.isoformat(), "currency_code": "SAR",
                "customer_request_date": (order_date - timedelta(days=5)).isoformat(),
                "standard_delivery_date": (order_date + timedelta(days=21)).isoformat(),
                "confirmed_delivery_date": (order_date + timedelta(days=14)).isoformat(),
                "actual_delivery_date": (order_date + timedelta(days=14)).isoformat(),
            })
            edges.append(_edge(order["id"], "FULFILLS", opp["node"]["id"]))
            posted = order_date.isoformat()
            added_orders += 1

            for item in chunk:
                line_seq += 1
                added_lines += 1
                line_id = f"OL{line_seq:06d}"
                value = round(item["quantity"] * item["unit_price"], 2)
                ln = node("OrderLine", {
                    "order_line_id": line_id, "order_id": order_id,
                    "product_id": item["product_id"], "quantity": item["quantity"],
                    "unit_price_recorded": item["unit_price"], "line_value": value,
                    "is_promotional": "true" if item["discount"] > 0.30 else "false",
                })
                edges.append(_edge(ln["id"], "PART_OF", order["id"]))
                edges.append(
                    _edge(ln["id"], "FOR_PRODUCT", product_node[item["product_id"]]["id"])
                )
                truth_rows.append({
                    "order_line_id": line_id, "product_id": item["product_id"],
                    "family": item["family"], "true_elasticity": item["true_elasticity"],
                    "list_price": item["list_price"], "unit_price": item["unit_price"],
                    "discount_pct": round(item["discount"] * 100, 2),
                    "quantity": item["quantity"], "period": month.strftime("%Y-%m"),
                })

                c = price_component("list_price", "level", "true",
                                    item["quantity"] * item["list_price"],
                                    "line", line_id, posted)
                edges.append(_edge(c["id"], "APPLIES_TO_LINE", ln["id"]))
                c = price_component("invoice_discount", "deduction", "true",
                                    -value * rng.uniform(0.005, 0.03), "line", line_id, posted)
                edges.append(_edge(c["id"], "APPLIES_TO_LINE", ln["id"]))
                if rng.random() < 0.7:
                    c = price_component("payment_discount", "deduction", "false",
                                        -value * rng.uniform(0.005, 0.02),
                                        "line", line_id, posted)
                    edges.append(_edge(c["id"], "APPLIES_TO_LINE", ln["id"]))
                if rng.random() < 0.6:
                    c = price_component("volume_rebate", "deduction", "false",
                                        -value * rng.uniform(0.005, 0.025),
                                        "line", line_id, posted)
                    edges.append(_edge(c["id"], "APPLIES_TO_LINE", ln["id"]))

            # Freight is posted against the order, which is why the on-invoice
            # band -- and every level below it -- is known at document grain.
            freight = price_component("freight", "deduction", "true",
                                      -rng.uniform(150, 1400), "order", order_id, posted)
            edges.append(_edge(freight["id"], "APPLIES_TO_ORDER", order["id"]))

    # Enquiries that produced nothing, so the funnel keeps roughly the shape it
    # arrived with rather than turning into a book of pure wins.
    for _ in range(round(added_orders * LOST_RATIO)):
        cust = customers[rng.randrange(len(customers))]
        created = window[rng.randrange(len(window))] + timedelta(days=rng.randrange(0, 27))
        opp = enquiry(cust, created, rng.randint(1, 4))
        close(opp, won=False)

    truth = {
        "generated_at": date.today().isoformat(),
        "seed": seed,
        "built_from": "the delivered AXIOM knowledge graph, kept whole",
        "orders_before": before_orders,
        "orders_added": added_orders,
        "orders_after": before_orders + added_orders,
        "lines_before": before_lines,
        "lines_added": added_lines,
        "reference_price": REFERENCE_PRICE,
        "true_elasticity_by_family": TRUE_ELASTICITY,
        "applies_to": "the added orders only; the 100 delivered orders are unchanged",
        "design": (
            f"a product x month panel over {months} months, each cell carrying a price "
            f"level drawn at random from a {DISCOUNT_RANGE[0]:.0%}-{DISCOUNT_RANGE[1]:.0%} "
            f"discount off the product's real list price, with {per_cell} lines sold at it"
        ),
        "quantity_model": (
            "quantity = scale_product * (unit_price / reference_price) ** "
            f"true_elasticity * exp(N(0, {LINE_NOISE_SD}))"
        ),
        "price_model": (
            "the month's price level is drawn before the demand it produces and "
            "independently of it, so the slope is identified"
        ),
        "acceptance_model": (
            f"P(win) = logistic(-{WIN_SLOPE} * (quote_price - rival_price) / list_price)"
        ),
        "lines": truth_rows,
    }

    label = (
        f"AXIOM PoC topped up: {before_orders} delivered orders kept whole, "
        f"{added_orders} synthetic orders added in the same catalogue"
    )
    return _document(doc.get("session_id") or session, label, nodes, edges), truth


def _add_months(anchor: date, n: int) -> date:
    """The first of the month `n` months after `anchor`."""
    month = anchor.month - 1 + n
    return date(anchor.year + month // 12, month % 12 + 1, 1)


def gate_report(doc: dict) -> str:
    """What the gates say about a book, without running the engine over it."""
    by_label: dict[str, list[dict]] = defaultdict(list)
    for n in doc["nodes"]:
        by_label[n["labels"][0]].append(n)

    orders = by_label["Order"]
    lines = by_label["OrderLine"]
    n_orders = len(orders)

    month_of = {
        n["properties"]["order_id"]: str(n["properties"].get("order_date", ""))[:7]
        for n in orders
    }
    per_scope: dict[str, set] = defaultdict(set)
    per_customer: dict[str, set] = defaultdict(set)
    customer_of = {n["properties"]["order_id"]: n["properties"]["customer_id"] for n in orders}
    for n in lines:
        oid = n["properties"].get("order_id")
        if oid in month_of:
            per_scope[n["properties"]["product_id"]].add(month_of[oid])
            per_customer[customer_of[oid]].add(oid)

    deep = sum(1 for months in per_scope.values() if len(months) >= 8)
    regulars = sum(1 for o in per_customer.values() if len(o) >= 5)

    rows = [
        ("documents", n_orders, 200, "optimisation needs 200, elasticity 120, segmentation 60"),
        ("products with 8+ months", deep, 1, "a product scope below 8 observations is not fitted"),
        ("customers with 5+ orders", regulars, 4, "4 clusters need at least 4 qualifying customers"),
    ]
    out = ["  what the gates will say:"]
    for name, have, need, why in rows:
        verdict = "ok" if have >= need else "SHORT"
        out.append(f"    {name:26s} {have:>6,}  (need {need:,})  {verdict:5s}  -- {why}")
    return "\n".join(out)


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="scenario", required=True)

    r = sub.add_parser("reduced", help="the delivered graph, cut down")
    r.add_argument("--orders", type=int, default=12,
                   help="how many of the delivered orders to keep (default 12)")
    r.add_argument("--drop-cost-periods", type=float, default=0.25,
                   help="fraction of product cost records to remove, so COGS becomes "
                        "unavailable on the lines that used them (default 0.25)")
    r.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--out", type=Path, default=None)

    t = sub.add_parser("topup", help="the delivered graph plus enough trade to pass")
    t.add_argument("--months", type=int, default=TOPUP_MONTHS,
                   help=f"months of added history per product (default {TOPUP_MONTHS})")
    t.add_argument("--lines-per-month", type=int, default=LINES_PER_CELL,
                   help=f"added lines per product per month (default {LINES_PER_CELL})")
    t.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    t.add_argument("--seed", type=int, default=20260919)
    t.add_argument("--out", type=Path, default=None)

    args = parser.parse_args()
    if not args.source.exists():
        print(f"Source export not found: {args.source}", file=sys.stderr)
        return 1
    print(f"Reading {args.source}")
    doc = json.loads(args.source.read_text(encoding="utf-8"))

    if args.scenario == "reduced":
        out = args.out or OUT_DIR / (
            f"axiom_reduced_{args.orders}"
            + (f"_gaps{int(args.drop_cost_periods * 100)}" if args.drop_cost_periods else "")
            + ".json"
        )
        built = reduce_export(doc, args.orders, args.drop_cost_periods, args.seed)
        _write(built, out)
        print()
        print(gate_report(built))
        return 0

    before = sum(1 for n in doc["nodes"] if n["labels"][0] == "Order")
    built, truth = topup(doc, args.seed, args.months, args.lines_per_month)
    out = args.out or OUT_DIR / f"axiom_topup_{truth['orders_after']}.json"
    _write(built, out)
    truth_path = out.with_name(out.stem + "_truth.json")
    truth_path.write_text(json.dumps(truth, indent=1), encoding="utf-8")

    print()
    print(gate_report(built))
    print()
    print(f"  delivered orders kept whole : {before:>6,}")
    print(f"  orders added                : {truth['orders_added']:>6,}")
    print(f"  book now                    : {truth['orders_after']:>6,}")
    print()
    print("  It took more than a top-up. The delivered book is 100 orders over 78")
    print("  products -- about three lines each -- and elasticity is fitted per")
    print("  product, so what was missing was never a handful of documents. It was")
    print("  depth in every scope, and a price that moves independently of demand.")
    print(f"\n  ground truth -> {truth_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
