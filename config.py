"""
Configuration globale du système de gestion de portefeuille crypto.
"""

# API
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COINGECKO_DELAY = 1.5  # secondes entre requêtes (rate limit gratuit)

# Chemins
PORTFOLIO_CSV = "portfolio.csv"
RAPPORTS_DIR = "rapports"
DB_PATH = "portfolio_history.db"

# Paramètres de risque
MAX_POSITION_PCT = 0.20       # alerte si > 20% du portefeuille
MIN_STABLECOIN_PCT = 0.15     # réserve liquidité minimale recommandée
MAX_AVG_CORRELATION = 0.75    # seuil alerte diversification
MIN_HISTORY_DAYS = 90         # historique minimum pour un nouvel actif
MAX_NEW_ASSET_PCT = 0.05      # max 5% sur actif sans historique suffisant

# Paramètres techniques
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD, BB_STD = 20, 2
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
MIN_RR_RATIO = 2.0            # ratio risque/rendement minimum

# Kelly Criterion
KELLY_FRACTION = 0.25         # Kelly partiel conservateur

# VaR
VAR_CONFIDENCE_LEVELS = [0.95, 0.99]
VAR_HORIZONS_DAYS = [1, 7]

# Historique OHLCV
OHLCV_DAYS = 365              # 1 an de données

# Stablecoins connus
STABLECOINS = {
    "usdt", "usdc", "dai", "busd", "tusd", "usdp",
    "usdd", "frax", "lusd", "susd", "gusd", "fdusd",
    "pyusd", "eur", "euroc"
}

# Mapping ticker -> CoinGecko ID (à compléter selon votre portefeuille)
TICKER_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "ATOM": "cosmos",
    "LTC": "litecoin",
    "DOGE": "dogecoin",
    "SHIB": "shiba-inu",
    "ARB": "arbitrum",
    "OP": "optimism",
    "INJ": "injective-protocol",
    "SUI": "sui",
    "APT": "aptos",
    "NEAR": "near",
    "FTM": "fantom",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
}
