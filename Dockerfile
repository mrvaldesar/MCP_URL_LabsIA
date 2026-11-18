FROM python:3.10-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d AS base

FROM base AS dependencies

WORKDIR /build
COPY requirements.txt .
RUN --mount=type=secret,id=pip_cert \
    if [ -f /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir -c requirements.txt \
        mcp pydantic starlette uvicorn httpx "PyJWT[crypto]"

FROM base AS runtime

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GRADER_LOG_PATH=/app/data/grader.log

RUN groupadd --gid 10001 grader \
    && useradd --uid 10001 --gid grader --no-create-home grader \
    && install -d -o grader -g grader /app/data

COPY --from=dependencies /opt/venv /opt/venv
COPY src/ src/
COPY migrations/ migrations/
RUN chmod -R a+rX /app/src /app/migrations

USER grader
EXPOSE 8000
CMD ["python", "-m", "src.server"]
