# Deliberately small, version-pinned base for the disposable diagnostic fixture.
FROM python:3.12.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash git curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/one
COPY pyproject.toml README.md /opt/one/
COPY one /opt/one/one
COPY scripts /opt/one/scripts
RUN python -m pip install --no-cache-dir --disable-pip-version-check .
ENTRYPOINT ["python", "/opt/one/scripts/diagnostic_workload.py"]
