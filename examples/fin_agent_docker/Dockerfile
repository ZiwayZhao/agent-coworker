FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir agent-coworker akshare

# Copy fin-agent code and knowledge base
COPY fin_agent.py .
COPY fin_knowledge/ ./fin_knowledge/

# Create data directory
RUN mkdir -p /root/.coworker

# Environment variables (set at runtime)
ENV DEEPSEEK_API_KEY=""
ENV FIN_AGENT_NAME="fin-agent"
ENV FIN_DATA_DIR="/root/.coworker"
ENV FIN_CACHE_DIR="/app/cache"

EXPOSE 8090

CMD ["python", "-u", "fin_agent.py"]
