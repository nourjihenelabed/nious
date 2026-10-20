# The AXIOM pricing engine as a container (D7).
#
#   docker build -t axiom-pricing .
#   docker run --rm -v "$PWD/data:/app/data" -v "$PWD/reports_axiom_v2:/app/reports_axiom_v2" \
#       -v axiom_mlflow:/mlflow axiom-pricing \
#       scripts/run_models.py --profile axiom_v2 --graph data/snapshot_axiom_v2 --out reports_axiom_v2
#
# The demo website runs from the same image:
#
#   docker run --rm -p 8765:8765 -v "$PWD/data:/app/data" -v "$PWD/reports_axiom_v2:/app/reports_axiom_v2" \
#       axiom-pricing demo/server.py --host 0.0.0.0
#
# then open http://localhost:8765.
#
# Any script in scripts/ runs the same way: the image's entry point is Python,
# the command is the script and its options. Data, reports and the MLflow store
# are mounted, never baked in, so one image serves every snapshot and every run
# is recorded where the MLflow UI can see it.
#
# Air-gapped by design: everything the engine needs is installed at build time,
# and nothing at run time calls out to a network.

FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MLFLOW_TRACKING_URI=sqlite:////mlflow/mlflow.db \
    MLFLOW_ARTIFACT_ROOT=/mlflow/artifacts

# PyTensor (behind PyMC) compiles its sampler with a C++ compiler; without one
# the elasticity model falls back to its conjugate form and says so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-docker.txt .
RUN pip install -r requirements-docker.txt

# The engine reads its settings from pyproject.toml next to src/, so the layout
# is kept as in the repository rather than installed into site-packages.
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY tests ./tests
COPY demo/server.py demo/explore.py ./demo/
COPY demo/static ./demo/static
EXPOSE 8765

RUN mkdir -p /mlflow /app/data
VOLUME ["/mlflow", "/app/data"]

ENTRYPOINT ["python"]
CMD ["scripts/run_models.py", "--help"]
