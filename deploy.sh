#!/bin/bash
# deploy.sh - Provisions a Vultr VPS and deploys the autobot trading bot
# Run this locally. It will create a VPS, upload setup_remote.sh, and execute it.
set -euo pipefail

VULTR_API_KEY="***REMOVED***"
REGION="ams"            # Amsterdam
PLAN="vc2-1c-1gb"       # Cheapest: 1 vCPU, 1GB RAM
LABEL="autobot-trader"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API="https://api.vultr.com/v2"

header() {
    echo -e "\n=== $1 ==="
}

vultr_api() {
    local method="$1"
    local endpoint="$2"
    shift 2
    curl -s -X "$method" "${API}${endpoint}" \
        -H "Authorization: Bearer ${VULTR_API_KEY}" \
        -H "Content-Type: application/json" \
        "$@"
}

# -------------------------------------------------------
# 1. Find Ubuntu 22.04 OS ID
# -------------------------------------------------------
header "Finding Ubuntu OS image"
OS_ID=$(vultr_api GET "/os" | jq -r '.os[] | select(.name | test("Ubuntu 22.04 x64"; "i")) | .id' | head -1)

if [ -z "$OS_ID" ] || [ "$OS_ID" = "null" ]; then
    echo "Could not find Ubuntu 22.04, trying Ubuntu 24.04..."
    OS_ID=$(vultr_api GET "/os" | jq -r '.os[] | select(.name | test("Ubuntu 24.04 x64"; "i")) | .id' | head -1)
fi

if [ -z "$OS_ID" ] || [ "$OS_ID" = "null" ]; then
    echo "ERROR: Could not find a suitable Ubuntu OS image."
    exit 1
fi
echo "OS ID: $OS_ID"

# -------------------------------------------------------
# 2. Verify plan and region are available
# -------------------------------------------------------
header "Verifying plan availability in $REGION"
PLAN_CHECK=$(vultr_api GET "/plans" | jq -r --arg plan "$PLAN" '.plans[] | select(.id == $plan) | .id')

if [ -z "$PLAN_CHECK" ] || [ "$PLAN_CHECK" = "null" ]; then
    echo "Plan $PLAN not found, listing cheapest available plans..."
    vultr_api GET "/plans" | jq -r '.plans[] | select(.type == "vc2") | "\(.id) \(.vcpu_count)cpu \(.ram)MB \(.monthly_cost)$/mo"' | sort -t'$' -k2 -n | head -5
    echo "Using first available cheap plan..."
    PLAN=$(vultr_api GET "/plans" | jq -r '[.plans[] | select(.type == "vc2")] | sort_by(.monthly_cost) | .[0].id')
fi
echo "Plan: $PLAN"

# -------------------------------------------------------
# 3. Provision the VPS
# -------------------------------------------------------
header "Provisioning VPS"
CREATE_RESPONSE=$(vultr_api POST "/instances" -d "{
    \"region\": \"${REGION}\",
    \"plan\": \"${PLAN}\",
    \"os_id\": ${OS_ID},
    \"label\": \"${LABEL}\",
    \"hostname\": \"autobot\",
    \"backups\": \"disabled\"
}")

INSTANCE_ID=$(echo "$CREATE_RESPONSE" | jq -r '.instance.id')

if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "null" ]; then
    echo "ERROR: Failed to create VPS instance."
    echo "Response: $CREATE_RESPONSE"
    exit 1
fi
echo "Instance ID: $INSTANCE_ID"

# -------------------------------------------------------
# 4. Wait for VPS to be ready
# -------------------------------------------------------
header "Waiting for VPS to become active"
MAX_WAIT=300
ELAPSED=0
STATUS="pending"

while [ "$STATUS" != "active" ] && [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    sleep 10
    ELAPSED=$((ELAPSED + 10))
    INSTANCE_DATA=$(vultr_api GET "/instances/${INSTANCE_ID}")
    STATUS=$(echo "$INSTANCE_DATA" | jq -r '.instance.status')
    POWER=$(echo "$INSTANCE_DATA" | jq -r '.instance.power_status')
    SERVER_STATE=$(echo "$INSTANCE_DATA" | jq -r '.instance.server_status')
    echo "  [${ELAPSED}s] status=$STATUS power=$POWER server=$SERVER_STATE"
done

if [ "$STATUS" != "active" ]; then
    echo "ERROR: VPS did not become active within ${MAX_WAIT}s"
    exit 1
fi

# Wait a bit more for server_status to be "ok" (SSH ready)
echo "Waiting for server to finish booting..."
SERVER_STATE="none"
while [ "$SERVER_STATE" != "ok" ] && [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    sleep 10
    ELAPSED=$((ELAPSED + 10))
    INSTANCE_DATA=$(vultr_api GET "/instances/${INSTANCE_ID}")
    SERVER_STATE=$(echo "$INSTANCE_DATA" | jq -r '.instance.server_status')
    echo "  [${ELAPSED}s] server_status=$SERVER_STATE"
done

# -------------------------------------------------------
# 5. Get IP and password
# -------------------------------------------------------
header "Retrieving connection details"
INSTANCE_DATA=$(vultr_api GET "/instances/${INSTANCE_ID}")
IP=$(echo "$INSTANCE_DATA" | jq -r '.instance.main_ip')
PASSWORD=$(echo "$INSTANCE_DATA" | jq -r '.instance.default_password')

echo "IP Address: $IP"
echo "Root Password: $PASSWORD"

# Save connection info locally
cat > "${SCRIPT_DIR}/vps_info.txt" << EOF
Instance ID: $INSTANCE_ID
IP Address:  $IP
Password:    $PASSWORD
Created:     $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
echo "Connection info saved to vps_info.txt"

# -------------------------------------------------------
# 6. Wait for SSH to be reachable
# -------------------------------------------------------
header "Waiting for SSH to be reachable"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o LogLevel=ERROR"
SSH_READY=0

for i in $(seq 1 30); do
    if sshpass -p "$PASSWORD" ssh $SSH_OPTS root@"$IP" "echo SSH_OK" 2>/dev/null | grep -q "SSH_OK"; then
        SSH_READY=1
        break
    fi
    echo "  Attempt $i/30 - SSH not ready yet..."
    sleep 10
done

if [ "$SSH_READY" -eq 0 ]; then
    echo "ERROR: Could not connect via SSH after 5 minutes."
    echo "You can try manually: sshpass -p '$PASSWORD' ssh root@$IP"
    exit 1
fi
echo "SSH is ready!"

# -------------------------------------------------------
# 7. Upload and execute setup script
# -------------------------------------------------------
header "Uploading setup script"
sshpass -p "$PASSWORD" scp $SSH_OPTS "${SCRIPT_DIR}/setup_remote.sh" root@"$IP":/root/setup_remote.sh

header "Running setup on VPS (this may take a few minutes)"
sshpass -p "$PASSWORD" ssh $SSH_OPTS root@"$IP" "chmod +x /root/setup_remote.sh && bash /root/setup_remote.sh" 2>&1 | tee "${SCRIPT_DIR}/deploy_log.txt"

# -------------------------------------------------------
# 8. Verify bot is running
# -------------------------------------------------------
header "Verifying bot status"
BOT_STATUS=$(sshpass -p "$PASSWORD" ssh $SSH_OPTS root@"$IP" "systemctl is-active autobot 2>/dev/null || echo 'inactive'")
echo "Bot service status: $BOT_STATUS"

header "Deployment Complete"
echo ""
echo "VPS IP:       $IP"
echo "Instance ID:  $INSTANCE_ID"
echo "Bot status:   $BOT_STATUS"
echo ""
echo "Useful commands:"
echo "  SSH in:           sshpass -p '$PASSWORD' ssh root@$IP"
echo "  View bot logs:    sshpass -p '$PASSWORD' ssh root@$IP 'tail -100 /var/log/autobot.log'"
echo "  Restart bot:      sshpass -p '$PASSWORD' ssh root@$IP 'systemctl restart autobot'"
echo "  Destroy VPS:      curl -s -X DELETE '${API}/instances/${INSTANCE_ID}' -H 'Authorization: Bearer ${VULTR_API_KEY}'"
