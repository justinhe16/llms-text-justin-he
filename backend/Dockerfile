# Backend image for Fly.io.
#
# One image serves both processes: the FastAPI API (the `app` process) and the ARQ worker
# (the `worker` process). Both commands are declared in fly.toml's `[processes]` block and
# each replaces the CMD below, so nothing here may assume this container is the API. In
# particular there is deliberately no HEALTHCHECK — an HTTP check baked into the image
# would fail forever in a worker container, which serves no HTTP. Fly's health checks are
# scoped to the `app` process in fly.toml.
#
# There is no Node in this image. Prisma runs migrations on the GitHub Actions runner,
# not here, which keeps ~150MB and a whole class of version drift out of the runtime.

FROM python:3.12-slim

# Unbuffered stdout so Fly's log shipper sees output as it happens; no .pyc files, since
# they would only bloat the layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: application code changes far more often than requirements.txt, and
# copying them separately keeps the slow install layer cached across those changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user. Site-packages stay root-owned, and therefore read-only to
# appuser, which is what we want: the runtime cannot rewrite its own dependencies.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Kept identical to fly.toml's `app` process command, so running this image locally and
# running it on Fly start the same way. A machine in the `worker` group replaces it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
