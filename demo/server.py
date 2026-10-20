"""AXIOM Pricing Module -- Track A demo server.

    python demo/server.py                    # http://127.0.0.1:8765
    python demo/server.py --port 9000 --profile axiom_v2 --graph data/snapshot_axiom_v2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import sys
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
_parser.add_argument("--host", default="127.0.0.1")
_parser.add_argument("--port", type=int, default=8765)
_parser.add_argument("--profile", default="axiom_v2")
_parser.add_argument("--graph", default=str(ROOT / "data" / "snapshot_axiom_v2"))
_parser.add_argument("--reports", default=str(ROOT / "reports_axiom_v2"),
                     help="folder with the latest DVC outputs (monitoring report, contract)")
_parser.add_argument("--cache", action="store_true",
                     help="keep the fitted run in demo/.cache between restarts (refitted when the "
                          "engine code or the data changes)")
ARGS, _ = _parser.parse_known_args()

# The engine reads its profile and graph once, at import.
os.environ["PRICING_PROFILE"] = ARGS.profile
os.environ["PRICING_GRAPH_JSON"] = str(Path(ARGS.graph).resolve())

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from explore import Explorer  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from pricing import __version__ as ENGINE_VERSION  # noqa: E402
from pricing import config  # noqa: E402
from pricing.contracts import schema as C  # noqa: E402
from pricing.engine import pipeline  # noqa: E402
from pricing.graph import get_source  # noqa: E402
from pricing.models import optimiser  # noqa: E402
from pricing.models.segmentation import structure_reading  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402
from pricing.tracking import registry  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
STATE: dict = {"ready": False, "error": None, "started": time.time(), "step": "starting"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def clean(value):
    """Make anything JSON-safe: numpy scalars to Python, NaN and inf to None."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, pd.DataFrame):
        return clean(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return clean(value.to_dict())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name") and not isinstance(value, str):
        return value.value  # Enum
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# start-up: run the engine once
# ---------------------------------------------------------------------------


def _cache_key() -> str:
    """Changes whenever the engine code, the settings or the data change."""
    h = hashlib.sha256()
    files = sorted((ROOT / "src" / "pricing").rglob("*.py")) + [ROOT / "pyproject.toml"]
    files += sorted(Path(ARGS.graph).rglob("*")) if Path(ARGS.graph).is_dir() else [Path(ARGS.graph)]
    for f in files:
        if f.is_file():
            h.update(str(f).encode())
            h.update(str(f.stat().st_mtime_ns).encode())
    h.update(ARGS.profile.encode())
    return h.hexdigest()[:16]


def _fit() -> object:
    profile = get_profile()
    src = get_source(profile=profile)
    STATE["step"] = "running the waterfall and fitting the models (about a minute)"
    return pipeline.run(src, profile=profile, with_models=True, track=False)


def load_engine() -> None:
    """Run the full pipeline with models and keep everything the pages need."""
    try:
        STATE["step"] = "reading and checking the graph"
        cache = Path(__file__).resolve().parent / ".cache" / f"run_{_cache_key()}.pkl"
        if ARGS.cache and cache.exists():
            STATE["step"] = "loading the cached run"
            run = pickle.loads(cache.read_bytes())
        else:
            run = _fit()
            if ARGS.cache:
                cache.parent.mkdir(exist_ok=True)
                cache.write_bytes(pickle.dumps(run))
        STATE.update(run=run, overview=build_overview(run), explorer=Explorer(run), step="ready")
        STATE["ready"] = True
    except Exception as exc:  # shown on the page, not swallowed
        traceback.print_exc()
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        STATE["step"] = "failed"


def build_overview(run) -> dict:
    """The model checks and settings shown on the "How it works" page."""
    totals, models, result = run.waterfall.totals, run.models, run.result
    seg, el, win = models.get("segmentation"), models.get("elasticity"), models.get("acceptance")
    overview = {
        "graph": {"documents": totals.get("n_transactions"), "lines": totals.get("n_lines")},
        "links": models.get("links", {}),
        "optimiser": {"rules": {
            "margin_floor": config.MARGIN_FLOOR,
            "min_win_probability": config.MIN_WIN_PROBABILITY,
            "min_volume_share": config.MIN_VOLUME_SHARE,
        }},
        "contract": {
            "version": result.provenance.contract_version,
            "waterfalls": len(result.waterfalls),
            "segments": len(result.segments),
            "elasticities": len(result.elasticities),
            "recommendations": len(result.recommendations),
            "violations": list(run.contract_violations),
        },
    }
    if seg is not None:
        overview["segmentation"] = {
            "note": seg.selection_note,
            "silhouette": seg.silhouette,
            "structure": structure_reading(seg.silhouette),
            "agreement": seg.method_agreement,
        }
    if el is not None:
        overview["elasticity"] = {
            "circularity": vars(el.circularity) if el.circularity else None,
            "estimates": [
                {"scope": e.scope, "beta": e.beta, "low": e.low, "high": e.high, "fit": e.fit_for_optimisation}
                for e in el.estimates
            ],
        }
    if getattr(win, "is_win_rate", False):
        overview["win"] = {
            "metrics": win.metrics,
            "price_effect": win.price_effect,
            "price_effect_sentence": win.price_effect_sentence(),
            "n_train": win.n_train,
            "n_test": win.n_test,
        }
    return clean(overview)


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------

app = FastAPI(title="AXIOM Pricing Module -- Track A demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _need_ready():
    if STATE.get("error"):
        raise HTTPException(500, STATE["error"])
    if not STATE["ready"]:
        raise HTTPException(503, f"The engine is still starting: {STATE['step']}.")
    return STATE["run"]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return clean(
        {
            "ready": STATE["ready"],
            "step": STATE["step"],
            "error": STATE.get("error"),
            "seconds": round(time.time() - STATE["started"]),
            "profile": ARGS.profile,
            "engine_version": ENGINE_VERSION,
            "contract_version": C.CONTRACT_VERSION,
            "pipeline_ok": STATE["run"].ok if STATE["ready"] else None,
        }
    )


@app.get("/api/overview")
def overview():
    _need_ready()
    return STATE["overview"]


@app.get("/api/options")
def options():
    _need_ready()
    return clean(STATE["explorer"].options())


class Filters(BaseModel):
    channel: list[str] = []
    region: list[str] = []
    customer_type: list[str] = []
    segment: list[str] = []
    account_class: list[str] = []
    industry: list[str] = []
    customer: list[str] = []
    start: str | None = None
    end: str | None = None


@app.post("/api/explore")
def explore(f: Filters):
    """The book under any combination of filters: waterfall, breakdowns, trend, customers."""
    _need_ready()
    d = f.model_dump()
    d["from"], d["to"] = d.pop("start"), d.pop("end")
    return clean(STATE["explorer"].view(d))


@app.get("/api/customer/{customer_id}")
def customer(customer_id: str):
    _need_ready()
    out = STATE["explorer"].customer(customer_id)
    if not out:
        raise HTTPException(404, f"No customer {customer_id}")
    return clean(out)


@app.get("/api/opportunity")
def opportunity():
    _need_ready()
    return clean(STATE["explorer"].opportunity())


@app.get("/api/pairs")
def pairs():
    """The customer-product pairs the book trades most, with their segment and status."""
    run = _need_ready()
    recs = run.models.get("recommendations")
    rows = []
    for _, r in recs.iterrows():
        rows.append(
            {
                "customer_id": r["customer_id"],
                "name": STATE["explorer"].customer_names.get(r["customer_id"], ""),
                "product_id": r["product_id"],
                "status": r["status"],
                "uplift_pct": r["uplift_pct"],
            }
        )
    return clean(rows)


class PriceRequest(BaseModel):
    customer_id: str
    product_id: str
    margin_floor: float = config.MARGIN_FLOOR
    min_win_probability: float = config.MIN_WIN_PROBABILITY
    min_volume_share: float = config.MIN_VOLUME_SHARE


@app.post("/api/price")
def price(req: PriceRequest):
    """Re-run the optimiser for one pair with the rules given, and trace the chain."""
    run = _need_ready()
    models = run.models
    win = models.get("acceptance")
    refs = models.get("references")
    data = models.get("win_data")
    el = models.get("elasticity")
    seg = models.get("segmentation")
    if win is None or refs is None or data is None:
        raise HTTPException(400, "This profile has no win/loss history, so there is no live optimiser.")

    rec, curve = optimiser.recommend_with_win_model(
        run.waterfall, win, refs, data.frame, req.customer_id, req.product_id,
        margin_floor=req.margin_floor,
        min_win_probability=req.min_win_probability,
        elasticity=el,
        min_volume_share=req.min_volume_share,
    )
    record = rec.to_contract(refs.currency, is_win_rate=True)

    # The chain, for this one pair.
    trace: dict = {}
    if seg is not None:
        row = seg.assignments[seg.assignments["customer_id"].astype(str) == req.customer_id]
        if len(row):
            r = row.iloc[0]
            trace["segment"] = {
                "label": r["segment_label"],
                "status": r["status"],
                "fit_score": r["segment_score"],
                "n_deals": r["n_deals"],
                "volume_tier": r["volume_tier"],
                "price_sensitivity_index": r["price_sensitivity_index"],
                "distinguishing": list(r["distinguishing_features"] or ()),
                "note": r["note"],
            }
    if el is not None:
        rule = optimiser.volume_rule_for(el, req.customer_id, req.product_id)
        trace["elasticity"] = {
            "applies": rule.applies, "beta": rule.beta, "low": rule.low, "high": rule.high,
            "scope": rule.scope, "skipped": rule.skipped,
        }
    context = data.frame[data.frame["customer_id"].astype(str) == req.customer_id]
    if len(context) and "segment" in context.columns:
        code = str(context["segment"].iloc[0])
        effect = (win.segment_price_effects or {}).get(code)
        trace["win"] = {
            "segment_code": code,
            "quotes_to_customer": len(context),
            "won_share": float(context["won"].mean()),
            "effect": effect,
            "warning": (win.segment_warnings or {}).get(code),
        }
    product = refs.product(req.product_id)
    if product is not None:
        trace["product"] = clean(product.drop(labels=["reference_ids"], errors="ignore").to_dict())

    return clean(
        {
            "recommendation": vars(rec),
            "curve": curve,
            "contract_record": asdict(record),
            "trace": trace,
            "currency": refs.currency,
        }
    )


@app.get("/api/contract")
def contract():
    """The full engine result Track B receives, as a download."""
    run = _need_ready()
    return Response(
        run.result.to_json(), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=engine_result.json"},
    )


@app.get("/api/mlops")
def mlops():
    """What the last DVC run left behind: monitoring, links, registry, DVC lock."""
    reports = Path(ARGS.reports)
    out: dict = {"reports_dir": str(reports)}
    for name in ("monitoring_report.txt", "model_win_report.txt"):
        path = reports / name
        out[name] = path.read_text(encoding="utf-8") if path.exists() else None
    contract_file = reports / "engine_result.json"
    if contract_file.exists():
        prov = json.loads(contract_file.read_text(encoding="utf-8")).get("provenance", {})
        out["last_contract_provenance"] = prov
    lock = ROOT / "dvc.lock"
    stages = yaml.safe_load(lock.read_text(encoding="utf-8")).get("stages", {}) if lock.exists() else {}
    out["dvc_stages"] = [{"stage": k, "cmd": v.get("cmd")} for k, v in stages.items()]
    try:
        out["registry"] = clean(registry.status(ARGS.profile))
    except Exception as exc:
        out["registry_error"] = str(exc)
    return clean(out)


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=load_engine, daemon=True).start()
    print(f"AXIOM Track A demo on http://{ARGS.host}:{ARGS.port}  (engine loading in the background)")
    uvicorn.run(app, host=ARGS.host, port=ARGS.port, log_level="warning")
