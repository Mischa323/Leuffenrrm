FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code, the launcher (handles TLS modes), and the agent payload.
COPY server/app ./app
COPY server/run.py ./run.py
COPY agent /agent

ENV RMM_DB_PATH=/data/rmm.db
# Default to HTTPS with a self-signed cert generated into the data volume.
ENV RMM_TLS_MODE=self-signed
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "run.py"]
