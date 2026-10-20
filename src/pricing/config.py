"""Configuration: pyproject.toml for defaults, environment variables to override."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_toml() -> dict[str, Any]:
    """Read [tool.axiom-pricing] from pyproject.toml, or {} if unreadable."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return {}
    path = REPO_ROOT / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("axiom-pricing", {})
    except Exception:
        return {}


SETTINGS = _load_toml()


def setting(path: str, default: Any = None, env: str | None = None) -> Any:
    """One setting, resolved: environment variable, then pyproject.toml, then default."""
    if env and (raw := os.getenv(env)) is not None:
        return raw
    node: Any = SETTINGS
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


DATA_DIR = REPO_ROOT / str(setting("data_dir", "data"))

#: Where every script writes.
REPORTS_DIR = REPO_ROOT / str(setting("reports_dir", "reports", env="PRICING_REPORTS_DIR"))
DOCS_DIR = REPO_ROOT / "docs"


def _default_snapshot() -> Path:
    """Newest `data/snapshot_*` directory, falling back to the hand-off location."""
    candidates = sorted(
        (p for p in DATA_DIR.glob("snapshot_*") if p.is_dir()),
        key=lambda p: p.name,
    )
    if candidates:
        return candidates[-1]
    return REPO_ROOT.parent / "synthetic"


DEFAULT_CSV_DIR = _default_snapshot()

SOURCE = str(setting("source", "csv", env="PRICING_SOURCE")).strip().lower()

#: Whether the backend was *chosen* or merely defaulted to.
SOURCE_IS_EXPLICIT = os.getenv("PRICING_SOURCE") is not None

CSV_DIR = Path(os.getenv("PRICING_DATA_DIR", str(DEFAULT_CSV_DIR)))

#: The AXIOM knowledge-graph export, read in place by the `graph_json` backend.
GRAPH_JSON = Path(
    os.getenv(
        "PRICING_GRAPH_JSON",
        str(
            setting("graph.export", "")
            or REPO_ROOT.parent / "axiom_pricing_module_Knowledge-graph_PoC.json"
        ),
    )
)

#: Which schema the engine is reading. See `pricing.profiles.REGISTRY`.
PROFILE = str(setting("profile", "b2b_industrial", env="PRICING_PROFILE")).strip()

#: How a measure that is known only at document grain is treated when the report is finer than
#: that.
GRAIN_POLICY = str(setting("waterfall.grain_policy", "declared_grain"))
#: Groups smaller than this are not reported as a number.
MIN_TRANSACTIONS = int(setting("waterfall.min_transactions", 3))
#: One cent. Currency, not statistics.
TOLERANCE = float(setting("waterfall.tolerance", 0.01))

#: Where a workbook-backed source and its mapping live, when PRICING_SOURCE=workbook.
SOURCE_MAPPING = os.getenv("PRICING_SOURCE_MAPPING", str(setting("mapping.file", "") or ""))
WORKBOOK = os.getenv("PRICING_WORKBOOK", str(setting("mapping.workbook", "") or ""))

ON_INVOICE = str(setting("discounts.on_invoice", "on-invoice"))
OFF_INVOICE = str(setting("discounts.off_invoice", "off-invoice"))

ALLOW_CROSS_CURRENCY = bool(setting("currency.allow_cross_currency", False))
REPORTING_CURRENCY = str(setting("currency.reporting_currency", "") or "")

#: Confidence bands.
CONFIDENCE_HIGH = float(setting("confidence.high", 0.80))
CONFIDENCE_MEDIUM = float(setting("confidence.medium", 0.50))
THIN_EVIDENCE_DEALS = int(setting("confidence.thin_evidence_deals", 10))

#: How much evidence each deliverable needs before its output means anything.
#: These were constants in `quality.readiness` until the PoC graph arrived with
#: a hundred orders in it and the honest answer to "can you fit an elasticity?"
#: became "not to the standard this module set itself". Moving them here does
#: not make that answer less true -- it makes the decision to proceed anyway a
#: declared one, recorded beside the run, rather than an edit to a source file
#: that nobody reading the output would ever see.
#:
#: `RECOMMENDED_DEPTH` is what the module asks for when it is not being argued
#: with. Anything below it is reported as a standing reservation on every model
#: fitted under it, and `readiness` says so in as many words.
#: Provenance per line, because these are not all the same kind of number.
#: `docs/CONSTANTS.md` carries the full register.
#:
#: - waterfall 1        SOUND. The waterfall is arithmetic; one document is one.
#: - margin_analytics 20  JUDGEMENT. Quartiles on fewer points are unstable, but
#:                        20 is a round number, not a result.
#: - segmentation 60    JUDGEMENT, and the literature says it is too low. Dolnicar,
#:                      Grun, Leisch & Schmidt (2014), Journal of Travel Research
#:                      53(3), require 70 x the number of segmentation variables.
#:                      `segmentation.FEATURES` holds 8, so the published figure is
#:                      560 *customers* -- a different unit and a far higher bar
#:                      than 60 documents. Segmentation on a book this size is a
#:                      proposal for discussion, whatever this threshold says.
#: - elasticity 120     SUPPORTED. Green (1991), Multivariate Behavioral Research
#:                      26(3), 499-510: N >= 104 + m for testing an individual
#:                      predictor. A log-log elasticity has m = 1, so N >= 105.
#:                      120 clears it. Do not lower this to 100; 100 does not.
#: - optimisation 200   JUDGEMENT. "More than elasticity, because it compounds two
#:                      estimates." There is no derivation.
RECOMMENDED_DEPTH: dict[str, int] = {
    "waterfall": 1,
    "margin_analytics": 20,
    "segmentation": 60,
    "elasticity": 120,
    "optimisation": 200,
}

MIN_DOCUMENTS: dict[str, int] = {
    name: int(setting(f"readiness.{name}", default, env=f"PRICING_MIN_{name.upper()}"))
    for name, default in RECOMMENDED_DEPTH.items()
}

#: Segmentation is the one deliverable that document count does not govern: it
#: clusters customers, so what binds is how many customers bought often enough
#: to have a habit. `SEGMENT_MIN_DEALS` is what counts as often enough, and
#: `SEGMENT_CLUSTERS` is how many groups are asked for -- the fit needs at least
#: that many qualifying customers or there is nothing to separate.
#:
#: Both are JUDGEMENT with no source. Four clusters is "the number a commercial
#: team can act on", which is a statement about meetings, not about the data; the
#: literature selects k by silhouette, gap statistic or stability across
#: resamples rather than asserting it. Lowering `SEGMENT_MIN_DEALS` to make a
#: thin book cluster removes the warning, not the thinness.
SEGMENT_MIN_DEALS = int(setting("readiness.segment_min_deals", 5, env="PRICING_SEGMENT_MIN_DEALS"))
SEGMENT_CLUSTERS = int(setting("readiness.segment_clusters", 4, env="PRICING_SEGMENT_CLUSTERS"))
RECOMMENDED_SEGMENT_MIN_DEALS = 5
#: How the number of segments is chosen.
SEGMENT_K_SELECTION = str(setting("models.segment_k", "auto", env="PRICING_SEGMENT_K"))
SEGMENT_K_RANGE = (2, 8)


#: The grain elasticity and acceptance are fitted at. Both hold a separate,
#: stricter rule of their own -- a scope needs `MIN_OBSERVATIONS` price/period
#: points, or `MIN_COMPARABLES` past deals, before it is fitted at all -- and
#: that rule is about one product's history, not about the size of the book. So
#: the readiness thresholds above can be met while every single scope abstains,
#: which is what a hundred orders spread over seventy-eight products does.
#:
#: The answer is to fit at a grain the data supports rather than to lower the
#: per-scope rule until noise passes it. `family` pools the same products a
#: commercial team already treats as substitutes. Note the asymmetry: a
#: concession *depth* pools across a family honestly, a *reference price* does
#: not, so a coarse acceptance scope makes the optimiser's bands wider and
#: blunter. See `docs/WEEK6_ACCEPTANCE_AND_OPTIMISATION.md`.
#:
#: `segment` (the default) measures elasticity inside each customer segment, so
#: the segmentation feeds the elasticity: see `models.elasticity.fit_by_segment`.
#: Any other value is a profile dimension (`product`, `family`, ...) and is
#: fitted per value of that dimension, as before.
ELASTICITY_SCOPE = str(setting("models.elasticity_scope", "segment", env="PRICING_ELASTICITY_SCOPE"))
ACCEPTANCE_SCOPE = str(setting("models.acceptance_scope", "product", env="PRICING_ACCEPTANCE_SCOPE"))

#: D6 price rules. The floor is cost plus this margin share; the volume rule is
#: the lowest chance of winning a recommended price may carry.
MARGIN_FLOOR = float(setting("models.margin_floor", 0.05, env="PRICING_MARGIN_FLOOR"))
MIN_WIN_PROBABILITY = float(
    setting("models.min_win_probability", 0.25, env="PRICING_MIN_WIN_PROBABILITY")
)
#: The volume rule.
MIN_VOLUME_SHARE = float(setting("models.min_volume_share", 0.80, env="PRICING_MIN_VOLUME_SHARE"))


def depth_reservations() -> tuple[str, ...]:
    """Every threshold currently set below what the module recommends."""
    notes = [
        f"{name}: running at {MIN_DOCUMENTS[name]} documents, below the recommended "
        f"{RECOMMENDED_DEPTH[name]}"
        for name in RECOMMENDED_DEPTH
        if MIN_DOCUMENTS[name] < RECOMMENDED_DEPTH[name]
    ]
    if SEGMENT_MIN_DEALS < RECOMMENDED_SEGMENT_MIN_DEALS:
        notes.append(
            f"segmentation: clustering customers with {SEGMENT_MIN_DEALS} deals, below "
            f"the recommended {RECOMMENDED_SEGMENT_MIN_DEALS}"
        )
    for name, scope in (("elasticity", ELASTICITY_SCOPE), ("acceptance", ACCEPTANCE_SCOPE)):
        if scope == "segment" and name == "elasticity":
            notes.append(
                "elasticity: one estimate per customer segment, with product effects "
                "removed inside each segment; every product a segment buys shares it"
            )
        elif scope != "product":
            notes.append(
                f"{name}: fitted at {scope} grain rather than product, so every product "
                f"in a {scope} shares one estimate"
            )
    return tuple(notes)


@dataclass(frozen=True)
class Neo4jSettings:
    """Connection settings for the read-only AXIOM knowledge graph."""

    uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.getenv("NEO4J_USER", "neo4j")
    password: str = os.getenv("NEO4J_PASSWORD", "")
    database: str = os.getenv("NEO4J_DATABASE", "neo4j")

    def is_configured(self) -> bool:
        """True once a password has been supplied."""
        return bool(self.password)


NEO4J = Neo4jSettings()

#: File names for the `b2b_industrial` CSV snapshot. `profiles.B2B_INDUSTRIAL`
#: reads this, so a host project that renames a file changes it here only.
CSV_FILES = {
    "transactions": "transactions.csv",
    "transaction_lines": "transaction_lines.csv",
    "transaction_discounts": "transaction_discounts.csv",
    "discounts": "discounts.csv",
    "customers": "customers.csv",
    "products": "products.csv",
    "channels": "channels.csv",
    "regions": "regions.csv",
}
