"""
Every Cypher query the engine uses, in one place.

Why a separate module: the queries are the *real* interface to AXIOM. Keeping
them here means (a) they can be reviewed by the NIOUS team without reading the
Python, (b) the CSV exporter and the live Neo4j reader use the exact same
queries, so the two backends can never drift apart.

Schema reference: Project Brief section 5 (entities, relationships, edge
properties). All queries are READ-ONLY -- no CREATE / MERGE / SET / DELETE
anywhere in this file, by design. See tests/test_cypher_readonly.py.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Node extraction -- one query per node label, returning a flat row shape that
# matches the CSV column headers exactly.
# --------------------------------------------------------------------------

TRANSACTIONS = """
MATCH (t:Transaction)
OPTIONAL MATCH (t)-[:PLACED_BY]->(c:Customer)
OPTIONAL MATCH (t)-[:SOLD_VIA]->(ch:Channel)
RETURN t.transaction_id AS transaction_id,
       t.date           AS date,
       c.customer_id    AS customer_id,
       ch.channel_id    AS channel_id,
       t.gross_amount   AS gross_amount,
       t.net_amount     AS net_amount,
       t.quantity       AS quantity,
       t.unit_price     AS unit_price,
       t.payment_terms  AS payment_terms
ORDER BY t.transaction_id
"""

CUSTOMERS = """
MATCH (c:Customer)
OPTIONAL MATCH (c)-[:LOCATED_IN]->(r:Region)
RETURN c.customer_id  AS customer_id,
       c.name         AS name,
       c.segment      AS segment,
       r.region_id    AS region_id,
       c.tier         AS tier,
       c.credit_terms AS credit_terms,
       c.tenure_years AS tenure_years,
       c.industry     AS industry,
       c.size         AS size
ORDER BY c.customer_id
"""

PRODUCTS = """
MATCH (p:Product)
RETURN p.product_id     AS product_id,
       p.name           AS name,
       p.category       AS category,
       p.sub_category   AS sub_category,
       p.list_price     AS list_price,
       p.unit_cost      AS unit_cost,
       p.product_family AS product_family
ORDER BY p.product_id
"""

DISCOUNTS = """
MATCH (d:Discount)
RETURN d.discount_id AS discount_id,
       d.type        AS type,
       d.category    AS category,
       d.rate        AS rate,
       d.amount      AS amount,
       d.valid_from  AS valid_from,
       d.valid_to    AS valid_to,
       d.trigger     AS trigger
ORDER BY d.discount_id
"""

CHANNELS = """
MATCH (ch:Channel)
RETURN ch.channel_id      AS channel_id,
       ch.type            AS type,
       ch.cost_to_serve   AS cost_to_serve,
       ch.commission_rate AS commission_rate
ORDER BY ch.channel_id
"""

REGIONS = """
MATCH (r:Region)
RETURN r.region_id   AS region_id,
       r.name        AS name,
       r.currency    AS currency,
       r.market_type AS market_type,
       r.country     AS country
ORDER BY r.region_id
"""

# --------------------------------------------------------------------------
# Relationship extraction -- edges that carry properties become their own
# "line item" tables, exactly as in the CSV snapshot.
# --------------------------------------------------------------------------

TRANSACTION_LINES = """
MATCH (t:Transaction)-[c:CONTAINS]->(p:Product)
RETURN t.transaction_id AS transaction_id,
       p.product_id     AS product_id,
       c.quantity       AS quantity,
       c.unit_price     AS unit_price,
       c.line_total     AS line_total
ORDER BY t.transaction_id, p.product_id
"""

TRANSACTION_DISCOUNTS = """
MATCH (t:Transaction)-[a:DISCOUNTED_BY]->(d:Discount)
RETURN t.transaction_id          AS transaction_id,
       d.discount_id             AS discount_id,
       a.discount_amount_applied AS discount_amount_applied
ORDER BY t.transaction_id, d.discount_id
"""

# Maps table name -> query. Used by both the exporter and the live reader.
QUERIES = {
    "transactions": TRANSACTIONS,
    "transaction_lines": TRANSACTION_LINES,
    "transaction_discounts": TRANSACTION_DISCOUNTS,
    "discounts": DISCOUNTS,
    "customers": CUSTOMERS,
    "products": PRODUCTS,
    "channels": CHANNELS,
    "regions": REGIONS,
}


# --------------------------------------------------------------------------
# Targeted queries -- used for the single-customer pull (the Week 1 quick win).
# Pushing the filter into Cypher instead of loading everything matters once the
# graph is production-sized.
# --------------------------------------------------------------------------

CUSTOMER_TRANSACTIONS = """
MATCH (t:Transaction)-[:PLACED_BY]->(c:Customer {customer_id: $customer_id})
OPTIONAL MATCH (t)-[:SOLD_VIA]->(ch:Channel)
RETURN t.transaction_id AS transaction_id,
       t.date           AS date,
       c.customer_id    AS customer_id,
       ch.channel_id    AS channel_id,
       t.gross_amount   AS gross_amount,
       t.net_amount     AS net_amount,
       t.quantity       AS quantity,
       t.unit_price     AS unit_price,
       t.payment_terms  AS payment_terms
ORDER BY t.date
"""

# --------------------------------------------------------------------------
# Profiling queries -- cheap graph-level counts we run before trusting anything.
# The CSV backend reproduces these numbers in pandas; the numbers must agree.
# --------------------------------------------------------------------------

NODE_COUNTS = """
MATCH (n)
RETURN labels(n)[0] AS label, count(*) AS n
ORDER BY label
"""

RELATIONSHIP_COUNTS = """
MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS n
ORDER BY relationship
"""

# Orphan check: transactions that do not resolve to a customer are a hard stop
# for the waterfall -- we must never silently aggregate them into a segment.
ORPHAN_TRANSACTIONS = """
MATCH (t:Transaction)
WHERE NOT (t)-[:PLACED_BY]->(:Customer)
RETURN count(t) AS orphan_transactions
"""
