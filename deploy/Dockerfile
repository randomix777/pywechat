# Product gateway image. Python 3.12+ is required (the gateway uses
# PEP 604 unions at runtime and a modern sqlite3).
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WECHAT_AI_PRODUCT_DB=/data/product.db

# The gateway only needs the standard library. No third-party runtime deps.
WORKDIR /app

# Install the source packages; requirements are not needed by the standard
# library gateway runtime.
COPY src/pyproject.toml src/setup.py src/setup.cfg src/README.md /app/src/
COPY src/wechat_ai_assistant /app/src/wechat_ai_assistant
COPY packages/business_agent/src/business_agent /app/src/business_agent

ENV PYTHONPATH=/app/src

# Run unprivileged.
RUN useradd --system --uid 10001 --create-home gateway \
    && mkdir -p /data && chown -R gateway:gateway /data /app
USER gateway

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "wechat_ai_assistant.gateway_server"]
