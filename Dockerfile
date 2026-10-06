# Container for running the load test from inside Azure (same region as the
# backends under test) instead of from a laptop over the public internet.
# All secrets come from Container Apps env vars at runtime -- never baked in.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY data ./data
COPY tools ./tools
COPY apps ./apps

RUN pip install --no-cache-dir ".[azure,azure-backends]"

EXPOSE 8080
CMD ["python", "apps/api/cloud_server.py"]
