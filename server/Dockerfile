FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code, the launcher (handles TLS modes), and the agent payload.
COPY server/app ./app
COPY server/run.py ./run.py
COPY VERSION ./VERSION
COPY agent /agent

# Version: the repo VERSION file is the default; a build arg (e.g. a git tag)
# overrides it. The app reads /app/VERSION so Settings shows the deployed build.
ARG RMM_VERSION=""
RUN if [ -n "$RMM_VERSION" ]; then printf '%s' "$RMM_VERSION" | sed 's/^v//' > /app/VERSION; fi

ENV RMM_DB_PATH=/data/rmm.db
# Default to HTTPS with a self-signed cert generated into the data volume.
ENV RMM_TLS_MODE=self-signed
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "run.py"]
