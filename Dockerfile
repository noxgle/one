FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

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

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/noxgle/one" \
    org.opencontainers.image.version=$VERSION \
    org.opencontainers.image.revision=$REVISION

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY docker/constraints.txt ./constraints.txt
COPY one ./one
RUN pip install --no-cache-dir --constraint constraints.txt . && pip check && rm -rf /build

WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["one"]
CMD []
