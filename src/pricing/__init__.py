"""
AXIOM Pricing Module -- Track A (Pricing Engine & MLOps).

Layers, outermost first:

    contracts/  the engine -> narrative result schema (D8). Track B's only import.
    engine/     the pricing maths: waterfall, aggregation, and later
                segmentation, elasticity, win-probability, optimiser.
    quality/    profiling and (Week 4) the Pricing Data Readiness Score.
    graph/      read-only access to the knowledge graph + the refuse-to-run gate.
    tracking/   MLflow experiment tracking and, from Week 7, the model registry.
    config.py   backend selection and domain constants.

Dependencies point inward only: graph knows nothing about engine, engine knows
nothing about tracking. `engine.pipeline` is the one place they meet.
"""

__version__ = "0.2.0"  # Week 2: D1 hardened, D2 waterfall + aggregation, D8 draft
