FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code, the launcher (handles TLS modes), and the agent payload.
COPY server/app ./app
COPY server/run.py ./run.py
COPY server/healthcheck.py ./healthcheck.py
COPY VERSION ./VERSION
COPY CHANGELOG.md ./CHANGELOG.md
COPY agent /agent
# Synology SPK packaging assets (INFO/scripts/conf); the server assembles the
# .spk on demand from these + the slim agent in /agent.
COPY packaging /packaging

# Version: the repo VERSION file is the default; a build arg (e.g. a git tag)
# overrides it. The app reads /app/VERSION so Settings shows the deployed build.
ARG RMM_VERSION=""
RUN if [ -n "$RMM_VERSION" ]; then printf '%s' "$RMM_VERSION" | sed 's/^v//' > /app/VERSION; fi
# The same number on the image itself, so a server checking for an update can
# say which version is waiting without starting it first.
LABEL org.opencontainers.image.version="${RMM_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Mischa323/Leuffenrrm"

ENV RMM_DB_PATH=/data/rmm.db
# Default to HTTPS with a self-signed cert generated into the data volume.
ENV RMM_TLS_MODE=self-signed
VOLUME ["/data"]
EXPOSE 8000

# Whether the server actually answers, over HTTPS or HTTP, on whatever port it
# was given. Docker and Synology show it, and an update from Settings waits for
# it before it throws the previous version away.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3   CMD ["python", "healthcheck.py"]

CMD ["python", "run.py"]
