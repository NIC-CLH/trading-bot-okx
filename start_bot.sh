#!/bin/bash
# Démarre le bot dans une session screen (survit à la déconnexion SSH)
cd /opt/trading_bot
source venv/bin/activate

# Tue l'ancienne session si elle existe
screen -S trading_bot -X quit 2>/dev/null || true

# Lance une nouvelle session détachée
screen -dmS trading_bot bash -c "
  cd /opt/trading_bot
  source venv/bin/activate
  export PYTHONIOENCODING=utf-8
  python3 scheduler.py
"

echo "✅ Bot démarré en arrière-plan (session screen 'trading_bot')"
echo ""
echo "Commandes utiles :"
echo "  screen -r trading_bot    → voir les logs en direct"
echo "  Ctrl+A puis D            → se détacher sans arrêter"
echo "  screen -ls               → lister les sessions actives"
