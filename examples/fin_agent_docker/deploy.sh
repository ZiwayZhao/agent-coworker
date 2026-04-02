#!/bin/bash
# Deploy fin-agent to server
# Usage: ./deploy.sh <server-ip> <deepseek-api-key>

SERVER=${1:-"47.245.143.61"}
API_KEY=${2:-""}
SSH_PASS="@Zzw89890151"

echo "=== Deploying fin-agent to $SERVER ==="

# 1. Copy files to server
echo "[1/4] Copying files..."
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no root@$SERVER "mkdir -p /root/fin-agent/fin_knowledge"
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no \
    ../fin_agent.py root@$SERVER:/root/fin-agent/
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no \
    ../fin_knowledge/rules.json ../fin_knowledge/system_prompt.md \
    root@$SERVER:/root/fin-agent/fin_knowledge/
sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no \
    Dockerfile root@$SERVER:/root/fin-agent/

# 2. Build Docker image on server
echo "[2/4] Building Docker image..."
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no root@$SERVER "
cd /root/fin-agent && docker build -t fin-agent:latest . 2>&1 | tail -5
"

# 3. Stop old container if exists
echo "[3/4] Starting container..."
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no root@$SERVER "
docker stop fin-agent 2>/dev/null
docker rm fin-agent 2>/dev/null
docker run -d --name fin-agent \
    --restart unless-stopped \
    -e DEEPSEEK_API_KEY=$API_KEY \
    -e FIN_AGENT_NAME=fin-agent \
    -v /root/.coworker:/root/.coworker \
    -p 8091:8090 \
    fin-agent:latest
"

# 4. Verify
echo "[4/4] Verifying..."
sleep 5
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no root@$SERVER "
docker ps | grep fin-agent
curl -s http://localhost:8091/api/health 2>/dev/null
"

echo ""
echo "=== Done! fin-agent deployed on port 8091 ==="
