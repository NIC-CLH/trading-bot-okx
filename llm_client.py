"""
Client Anthropic partagé pour les agents LLM du bot.

Lit la clé directement depuis .env pour éviter le conflit avec
ANTHROPIC_BASE_URL du proxy Claude Code qui écrase la variable d'env.
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent
_client: anthropic.Anthropic | None = None


def _load_api_key() -> str:
    """Lit ANTHROPIC_API_KEY depuis .env en priorité, puis os.environ."""
    env_file = _BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    return os.getenv("ANTHROPIC_API_KEY", "")


def get_client() -> anthropic.Anthropic:
    """Retourne le client Anthropic (singleton). Lève ValueError si clé absente."""
    global _client
    if _client is None:
        api_key = _load_api_key()
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY absent du .env — agents LLM désactivés"
            )
        _client = anthropic.Anthropic(
            api_key=api_key,
            base_url="https://api.anthropic.com",
        )
        logger.debug("[LLM] Client Anthropic initialisé")
    return _client


def is_available() -> bool:
    """Vérifie si la clé API est configurée sans lever d'exception."""
    try:
        get_client()
        return True
    except ValueError:
        return False
