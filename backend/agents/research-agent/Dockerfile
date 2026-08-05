# Research agent — CONTAINER deployment.
#
# The Foundry hosted agent platform requires x86_64 (linux/amd64) images.
# `docker.remoteBuild: true` in azure.yaml makes ACR perform the build, which is
# the supported path. If you ever build locally on an ARM host you MUST pass:
#
#     docker build --platform linux/amd64 -t research-agent:dev .
#
# Multi-stage: build deps into a venv, then copy only the venv into a clean
# runtime layer so no compiler toolchain or pip cache ships in the final image.

# --------------------------------------------------------------------------
# Stage 1 — dependency build
# --------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-virtualenv -r requirements.txt

# --------------------------------------------------------------------------
# Stage 2 — runtime
# --------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged, non-login user.
RUN groupadd --system --gid 10001 appgroup \
    && useradd --system --uid 10001 --gid appgroup \
        --home-dir /app --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appgroup main.py ./
COPY --chown=appuser:appgroup research_agent/ ./research_agent/

USER appuser

# The protocol adapter serves /responses and /readiness on 8088.
EXPOSE 8088

CMD ["python", "main.py"]
