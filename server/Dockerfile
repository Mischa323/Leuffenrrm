FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code and the agent payload the server distributes.
COPY server/app ./app
COPY agent /agent

ENV RMM_DB_PATH=/data/rmm.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
