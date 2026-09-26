# Disposable, offline write-tool diagnostic fixture.
FROM python:3.12.11-slim-bookworm
WORKDIR /opt/one
COPY pyproject.toml README.md /opt/one/
COPY one /opt/one/one
COPY scripts/write_tool_workload.py scripts/fake_openai_provider.py /opt/one/scripts/
RUN python -m pip install --no-cache-dir --disable-pip-version-check .
ENTRYPOINT ["python", "/opt/one/scripts/write_tool_workload.py"]
