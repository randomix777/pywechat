# Product gateway image. Python 3.12+ is required (the gateway uses
# PEP 604 unions at runtime and a modern sqlite3).
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYWEIXIN_PRODUCT_DB=/data/product.db

# The gateway only needs the standard library. No third-party runtime deps.
WORKDIR /app

# Install just the package sources; requirements are not needed by the gateway
# but the package layout keeps `pyweixin` importable.
COPY src/pyproject.toml src/setup.py src/setup.cfg src/README.md /app/src/
COPY src/pyweixin/__init__.py src/pyweixin/product.py src/pyweixin/gateway.py \
     src/pyweixin/gateway_server.py src/pyweixin/opencode.py src/pyweixin/webui.py \
     src/pyweixin/orders.py src/pyweixin/agent.py src/pyweixin/reliability.py \
     src/pyweixin/cloud.py /app/src/pyweixin/

ENV PYTHONPATH=/app/src

# Run unprivileged.
RUN useradd --system --uid 10001 --create-home gateway \
    && mkdir -p /data && chown -R gateway:gateway /data /app
USER gateway

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "pyweixin.gateway_server"]
