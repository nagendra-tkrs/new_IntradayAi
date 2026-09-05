"""Static NSE sector map used as a reliable fallback for company sector display.

This data is copied from the project's own existing provider metadata
(mock_provider.NIFTY50_STOCKS and kite_provider.NIFTY50_INSTRUMENTS) so there is
a single source of truth. It is NOT AI-guessed; it is the already-available
company metadata shipped with the project.

The yfinance provider tries the live Yahoo Finance quoteSummary API first and
falls back to this map so that companies show their real sector even when Yahoo
is unreachable / rate-limited / geo-blocked. "Unknown" is returned only when a
symbol is genuinely not present in any reliable data source.
"""

# Symbol (base, without .NS) -> sector
NSE_SECTOR_MAP = {
    "RELIANCE": "Oil & Gas",
    "TCS": "IT",
    "HDFCBANK": "Banking",
    "INFY": "IT",
    "ICICIBANK": "Banking",
    "HINDUNILVR": "FMCG",
    "SBIN": "Banking",
    "BHARTIARTL": "Telecom",
    "KOTAKBANK": "Banking",
    "ITC": "FMCG",
    "LT": "Infrastructure",
    "AXISBANK": "Banking",
    "BAJFINANCE": "Finance",
    "MARUTI": "Auto",
    "TATAMOTORS": "Auto",
    "SUNPHARMA": "Pharma",
    "ASIANPAINT": "Consumer",
    "HCLTECH": "IT",
    "TITAN": "Consumer",
    "ADANIENT": "Conglomerate",
    "WIPRO": "IT",
    "ULTRACEMCO": "Cement",
    "ONGC": "Oil & Gas",
    "TATASTEEL": "Metals",
    "NTPC": "Power",
    "POWERGRID": "Power",
    "BAJAJFINSV": "Finance",
    "NESTLEIND": "FMCG",
    "TECHM": "IT",
    "DRREDDY": "Pharma",
    "COALINDIA": "Mining",
    "BAJAJ-AUTO": "Auto",
    "HEROMOTOCO": "Auto",
    "DIVISLAB": "Pharma",
    "BRITANNIA": "FMCG",
    "CIPLA": "Pharma",
    "EICHERMOT": "Auto",
    "APOLLOHOSP": "Healthcare",
    "GRASIM": "Cement",
    "JSWSTEEL": "Metals",
    "M&M": "Auto",
    "ADANIPORTS": "Infrastructure",
    "INDUSINDBK": "Banking",
    "HDFCLIFE": "Insurance",
    "SBILIFE": "Insurance",
    "TATACONSUM": "FMCG",
    "HONAUT": "Manufacturing",
    "DABUR": "FMCG",
    "COLPALPH": "FMCG",
    "MARICO": "FMCG",
    "ICICIPRULI": "Insurance",
    "SHRIRAMFIN": "Financial Services",
    "LICI": "Insurance",
    "IRCTC": "Services",
    "PIDILITIND": "Consumer",
    "ASTRAL": "Manufacturing",
    "TRENT": "Retail",
    "PERSISTENT": "IT",
    "COFORGE": "IT",
    "MPHASIS": "IT",
    "MINDTREE": "IT",
    "BANDHANBNK": "Banking",
    "FEDERALBNK": "Banking",
    "PNB": "Banking",
    "IDFCFIRSTB": "Banking",
}

# Additional name information for symbols not covered by the sector map.
NSE_NAME_MAP = {
    "M&M": "Mahindra & Mahindra",
    "ADANIPORTS": "Adani Ports",
    "HDFCLIFE": "HDFC Life",
    "SBILIFE": "SBI Life",
    "TATACONSUM": "Tata Consumer Products",
    "HONAUT": "Honeywell Automation",
    "DABUR": "Dabur India",
    "COLPALPH": "Colgate-Palmolive India",
    "MARICO": "Marico",
    "ICICIPRULI": "ICICI Prudential Life",
    "SHRIRAMFIN": "Shriram Finance",
    "LICI": "Life Insurance Corp",
    "IRCTC": "Indian Railway Catering",
    "PIDILITIND": "Pidilite Industries",
    "ASTRAL": "Astral",
    "TRENT": "Trent",
    "PERSISTENT": "Persistent Systems",
    "COFORGE": "Coforge",
    "MPHASIS": "Mphasis",
    "MINDTREE": "LTIMindtree",
    "BANDHANBNK": "Bandhan Bank",
    "FEDERALBNK": "Federal Bank",
    "PNB": "Punjab National Bank",
    "IDFCFIRSTB": "IDFC First Bank",
}


def get_sector(symbol: str) -> str:
    """Return the static sector for a base symbol, or 'Unknown' if not present."""
    key = (symbol or "").upper()
    if key.endswith(".NS"):
        key = key[:-3]
    return NSE_SECTOR_MAP.get(key, "Unknown")


def get_name(symbol: str) -> str:
    """Return a static display name for a base symbol, or the symbol itself."""
    key = (symbol or "").upper()
    if key.endswith(".NS"):
        key = key[:-3]
    return NSE_NAME_MAP.get(key, symbol)
