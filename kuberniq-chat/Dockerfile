FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py .
COPY auth.py .

# Data directory — persists users.json and admin-initial-password.txt
# Mount a volume here in production:  -v kuberniq-chat-data:/data
RUN mkdir -p /data
ENV KUBERNIQ_DATA_DIR=/data

# Streamlit config — disable telemetry, set server options
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Secrets injected at runtime via env — never baked into the image
# OPENAI_API_KEY   — required
# MCP_SERVER_URL   — required (e.g. http://mcp-server.mcp-server.svc.cluster.local:8080)
# MCP_USERNAME     — MCP server service-account username
# MCP_PASSWORD     — MCP server service-account password
# KUBERNIQ_DATA_DIR — path to auth data dir (default: /data)

EXPOSE 8501

ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
