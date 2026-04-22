#!/bin/bash
# Script d'installation automatique du bot de trading sur VPS Ubuntu
# Usage : bash deploy_vps.sh

set -e
echo "=========================================="
echo "  Installation du bot de trading OKX"
echo "=========================================="

# Mise à jour système
apt-get update -y && apt-get upgrade -y

# Python 3.11
apt-get install -y python3.11 python3.11-pip python3.11-venv git screen

# Dossier du bot
mkdir -p /opt/trading_bot
cd /opt/trading_bot

# Environnement virtuel Python
python3.11 -m venv venv
source venv/bin/activate

# Dépendances
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=========================================="
echo "  Installation terminée !"
echo "  Lance le bot avec : bash start_bot.sh"
echo "=========================================="
