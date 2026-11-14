# The All Clear API as a container (Fly.io, Railway, any Docker host).
#
#   cd level-a
#   docker build -f deploy/Dockerfile -t levela-api .
#   docker run --rm --env-file .env -e API_BASE_URL=http://127.0.0.1:8000 -p 8000:8000 levela-api
#
# Settings come from the environment (the host's dashboard, or --env-file);
# nothing secret is baked into the image: only the directories below are
# copied, and deploy/Dockerfile.dockerignore keeps .env out of the context.
# Only the API runs here. The worker, the supervisor and the order systems run on
# the team laptop (deploy/README.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# What the API imports: the engine and the scenarios /admin/recalls starts.
# The retired seed kit and historical dataset are no longer dependencies.
COPY levela ./levela
COPY scenarios ./scenarios

# Root owns the code; the server runs as an unprivileged user that can only read it.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin levela
USER levela

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"]

# A shell, so the host's $PORT is honoured; exec, so the server gets the signals.
CMD ["sh", "-c", "exec python -m levela api --host 0.0.0.0 --port \"${PORT:-8000}\""]
