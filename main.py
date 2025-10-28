from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import httpx
import time
from typing import Optional, Dict, Any

# --- CENTRAL CONFIGURATION ---
# MIGRATION: Switched from exchangerate.host to ExchangeRate-API (Open Access)
# This public endpoint does not require an API key and is more suitable for
# a monetizable API gateway.
FIAT_API_BASE_URL = "https://open.er-api.com/v6/latest/"
COINGECKO_API_BASE_URL = "https://api.coingecko.com/api/v3/"

# Simple in-memory TTL cache
CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 60 * 15  # Cache rates for 15 minutes (900s) to reduce external API calls.

# --- FASTAPI SETUP ---
app = FastAPI(
    title="Currency & Crypto Converter API (Monetizable Version)",
    description="Convert fiat currencies and cryptocurrencies using robust public APIs (ExchangeRate-API & CoinGecko).",
    version="2.0.0"
)

# --- UTILITIES ---
def cache_get(key: str):
    entry = CACHE.get(key)
    if not entry:
        return None
    if time.time() > entry["expires_at"]:
        CACHE.pop(key, None)
        return None
    return entry["value"]


def cache_set(key: str, value: Any, ttl: int = CACHE_TTL_SECONDS):
    CACHE[key] = {"value": value, "expires_at": time.time() + ttl}


# Mapping common crypto symbols to CoinGecko ids
CRYPTO_SYMBOL_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
    "BNB": "binancecoin",
    "ADA": "cardano",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "DOT": "polkadot",
    "LTC": "litecoin",
    "USDC": "usd-coin",        
    "DAI": "dai",              
    "TRX": "tron",             
    "SHIB": "shiba-inu",       
    "AVAX": "avalanche-2",     
    "LINK": "chainlink",       
    "UNI": "uniswap",          
    "BCH": "bitcoin-cash",     
    "MATIC": "polygon",        
    "HBAR": "hedera-hashgraph", 
    # add more if needed
}

# --- DATA FETCHING HELPERS (Monetizable) ---

# Helper: get fiat rate from ExchangeRate-API (Open Access)
async def get_fiat_rate(base: str, target: str) -> float:
    base_u = base.upper()
    target_u = target.upper()
    cache_key = f"fiat:{base_u}:{target_u}"
    cached_rate = cache_get(cache_key)
    if cached_rate is not None:
        return cached_rate

    # URL example: https://open.er-api.com/v6/latest/USD
    url = f"{FIAT_API_BASE_URL}{base_u}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status() # Raises HTTPStatusError for 4xx/5xx

        data = resp.json()
        
        if data.get("result") != "success":
            # Handle errors if the ExchangeRate-API returns an error
            raise HTTPException(status_code=502, detail=f"ExchangeRate-API failed: {data.get('error') or 'Unknown error'}")

        # The rate for the target currency is inside the 'rates' dictionary
        rates = data.get("rates")
        if not rates:
            raise HTTPException(status_code=502, detail="Unexpected response structure from ExchangeRate-API")
        
        rate = rates.get(target_u)
        
        if rate is None:
            # This can occur if the currency code (e.g., EUD) is invalid
            raise HTTPException(status_code=400, detail=f"Invalid currency code: {target_u}")
        
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch fiat rates (HTTP Error {e.response.status_code})")
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"Timeout or network error connecting to ExchangeRate-API: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error while processing fiat rates: {str(e)}")

    # Cache the rate
    cache_set(cache_key, rate)
    return rate


# Helper: get crypto price from CoinGecko 
async def get_crypto_price(symbol: str, vs_currency: str) -> float:
    symbol_upper = symbol.upper()
    vs_currency_lower = vs_currency.lower()
    coin_id = CRYPTO_SYMBOL_MAP.get(symbol_upper)
    cache_key = f"crypto:{symbol_upper}:{vs_currency_lower}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            # Attempt 1: use the mapped ID
            if coin_id:
                url = f"{COINGECKO_API_BASE_URL}simple/price?ids={coin_id}&vs_currencies={vs_currency_lower}"
                resp = await client.get(url)
                resp.raise_for_status() 
                data = resp.json()
                price = data.get(coin_id, {}).get(vs_currency_lower)
                if price is not None:
                    cache_set(cache_key, price)
                    return price
            
            # Attempt 2 (Fallback): Find coin id by symbol (Slower, cached)
            if not coin_id:
                list_cache_key = f"coingecko:coins:list"
                coins_list = cache_get(list_cache_key)
                
                if coins_list is None:
                    resp = await client.get(f"{COINGECKO_API_BASE_URL}coins/list")
                    resp.raise_for_status() 
                    coins_list = resp.json()
                    cache_set(list_cache_key, coins_list, ttl=3600 * 24)  # Cache the list for 24 hours
                
                coin_id = next((c["id"] for c in coins_list if c["symbol"].upper() == symbol_upper), None)
                
                if not coin_id:
                    raise HTTPException(status_code=404, detail=f"Crypto symbol '{symbol_upper}' not found on CoinGecko")
                
                # Now fetch the price with the found coin_id
                url = f"{COINGECKO_API_BASE_URL}simple/price?ids={coin_id}&vs_currencies={vs_currency_lower}"
                resp = await client.get(url)
                resp.raise_for_status() 
                data = resp.json()
                price = data.get(coin_id, {}).get(vs_currency_lower)

            if price is None:
                raise HTTPException(status_code=502, detail="CoinGecko returned no price for requested vs_currency")
    
    except httpx.HTTPStatusError as e:
        # This catches 429 errors (Rate Limit) from CoinGecko, vital for monetization
        if e.response.status_code == 429:
             raise HTTPException(status_code=429, detail="Rate limit exceeded on CoinGecko. Increase cache TTL or upgrade CoinGecko plan.")
        raise HTTPException(status_code=502, detail=f"Failed to fetch crypto price (HTTP Error {e.response.status_code})")
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"Timeout or network error connecting to CoinGecko: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error while processing crypto rates: {str(e)}")

    cache_set(cache_key, price)
    return price

# --- ENDPOINTS ---

@app.get("/")
def root():
    return {"message": "Currency & Crypto Converter API (Monetizable) is running."}


@app.get("/convert")
async def convert(
    from_currency: str = Query(..., description="Source fiat currency code, e.g. USD"),
    to_currency: str = Query(..., description="Target fiat currency code, e.g. EUR"),
    amount: float = Query(1.0, gt=0, description="Amount to convert")
):
    """
    Convert fiat currency using ExchangeRate-API (Open Access).
    Example: /convert?from_currency=USD&to_currency=EUR&amount=100
    """
    try:
        rate = await get_fiat_rate(from_currency, to_currency)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    converted = round(amount * rate, 8)
    return {
        "from": from_currency.upper(),
        "to": to_currency.upper(),
        "amount": amount,
        "rate": rate,
        "converted_amount": converted
    }


@app.get("/crypto")
async def crypto_convert(
    symbol: str = Query(..., description="Crypto symbol, e.g. BTC"),
    vs_currency: str = Query("usd", description="Fiat to compare to, e.g. usd"),
    amount: float = Query(1.0, gt=0, description="Amount of crypto to convert")
):
    """
    Convert crypto to fiat using CoinGecko.
    Example: /crypto?symbol=BTC&vs_currency=usd&amount=0.5
    """
    try:
        price = await get_crypto_price(symbol, vs_currency)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    converted = round(amount * price, 8)
    return {
        "symbol": symbol.upper(),
        "vs_currency": vs_currency.lower(),
        "amount": amount,
        "price_per_unit": price,
        "converted_amount": converted
    }