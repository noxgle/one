FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/one/bin:$PATH \
    HOME=/config \
    ONE_CODING_AGENT_DIR=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git openssh-client ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/one \
    && useradd --create-home --uid 1000 one \
    && mkdir -p /workspace /config \
    && chown one:one /workspace /config

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY one ./one
RUN pip install --no-cache-dir . && rm -rf /build

WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["one"]
CMD []
