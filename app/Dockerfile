# FraudGraph console — single Cloud Run container (design doc §1.7):
# Streamlit console + in-process ADK 2.x runner + MCP Toolbox server on localhost.
# Phase 0: console-only hello-world revision (early deploy, risk-register item).
# Phase 2/3: add the Toolbox binary + supervisor line below.

FROM python:3.11-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Phase 2: MCP Toolbox server binary (pin the version when wiring):
# ADD https://storage.googleapis.com/genai-toolbox/v0.18.0/linux/amd64/toolbox /usr/local/bin/toolbox
# RUN chmod +x /usr/local/bin/toolbox

COPY app/ ./app/

ENV PORT=8080 PYTHONUNBUFFERED=1
# Phase 2/3 CMD becomes: toolbox --tools-file app/agents/tools.yaml & streamlit ...
CMD ["sh", "-c", "streamlit run app/console/streamlit_app.py --server.port ${PORT} --server.address 0.0.0.0 --server.headless true"]
