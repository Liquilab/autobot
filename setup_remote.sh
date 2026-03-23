#!/bin/bash
# setup_remote.sh - Runs ON the VPS to configure the trading bot environment
set -euo pipefail

echo "=== Autobot VPS Setup ==="

# Update system
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

# Install Python 3.11+, pip, git, and build dependencies
apt-get install -y python3 python3-pip python3-venv git build-essential \
    software-properties-common curl jq

# Check python version, install 3.11 from deadsnakes if needed
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo "Python version too old ($PYTHON_VERSION), installing 3.11..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y python3.11 python3.11-venv python3.11-dev
    PYTHON_BIN=python3.11
else
    PYTHON_BIN=python3
fi

echo "Using Python: $($PYTHON_BIN --version)"

# Clone repo
REPO_DIR="/opt/autobot"
PAT="***REMOVED***"

if [ -d "$REPO_DIR" ]; then
    echo "Repo already exists, pulling latest..."
    cd "$REPO_DIR"
    git pull
else
    echo "Cloning repo..."
    git clone "https://${PAT}@github.com/Liquilab/autobot.git" "$REPO_DIR"
fi

cd "$REPO_DIR"

# Configure git for pushing
git config user.email "autobot@liquilab.io"
git config user.name "Autobot"
git remote set-url origin "https://${PAT}@github.com/Liquilab/autobot.git"

# Create virtual environment and install dependencies
echo "Setting up Python venv..."
$PYTHON_BIN -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create .env file
echo "Creating .env file..."
cat > .env << 'ENVEOF'
PRIVATE_KEY=***REMOVED***
WALLET_ADDRESS=0x13bcc01fAA179FDa6556aab4A652B94C937dC126
FUNDER_ADDRESS=0x1240Ff4f31BF4e872d4700363Cc6EE2D11CCeec2
POLYGON_RPC=https://polygon-bor-rpc.publicnode.com
ENVEOF

chmod 600 .env

# Create systemd service
echo "Creating systemd service..."
cat > /etc/systemd/system/autobot.service << SVCEOF
[Unit]
Description=Autobot Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/autobot
Environment=PATH=/opt/autobot/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=/opt/autobot/.env
ExecStart=/opt/autobot/venv/bin/python /opt/autobot/src/autonomous_bot.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/autobot.log
StandardError=append:/var/log/autobot-error.log

[Install]
WantedBy=multi-user.target
SVCEOF

# Create log rotation
cat > /etc/logrotate.d/autobot << 'LOGEOF'
/var/log/autobot.log /var/log/autobot-error.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
LOGEOF

# Enable and start the service
systemctl daemon-reload
systemctl enable autobot.service
systemctl start autobot.service

echo ""
echo "=== Setup Complete ==="
echo "Bot status: $(systemctl is-active autobot.service)"
echo "View logs:  journalctl -u autobot -f"
echo "Or:         tail -f /var/log/autobot.log"
echo "Restart:    systemctl restart autobot"
