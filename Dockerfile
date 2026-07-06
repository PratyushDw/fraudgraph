# FraudGraph console - single Cloud Run container.
# Streamlit console + in-process ADK runner + MCP Toolbox server on localhost.
# Lives at the repo root (not app/) so `gcloud run deploy --source .` builds with
# a context that has both requirements.txt and app/. The BigQuery-backed console
# is fully usable on its own; the Toolbox + Vertex path powers the live
# "Generate case file" button.

FROM python:3.11-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir google-cloud-bigquery-storage

# MCP Toolbox server binary (Linux) — parameterized-SQL tool layer for the agents.
RUN curl -fsSL -o /usr/local/bin/toolbox \
      https://storage.googleapis.com/genai-toolbox/v1.1.0/linux/amd64/toolbox \
    && chmod +x /usr/local/bin/toolbox

COPY app/ ./app/

ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    FRAUDGRAPH_PROJECT_ID=fraudgraph \
    GOOGLE_CLOUD_PROJECT=fraudgraph \
    GOOGLE_CLOUD_LOCATION=global \
    GOOGLE_GENAI_USE_VERTEXAI=TRUE \
    TOOLBOX_URL=http://127.0.0.1:5000

# Start the Toolbox server on localhost, then the Streamlit console on $PORT.
# Toolbox failing must not stop the console — it only disables the optional
# live-generation button; the BigQuery-backed UI runs regardless.
CMD ["sh", "-c", "toolbox --config app/agents/tools.yaml --address 127.0.0.1 --port 5000 --enable-api & streamlit run app/console/streamlit_app.py --server.port ${PORT} --server.address 0.0.0.0 --server.headless true"]
