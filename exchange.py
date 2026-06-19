import datetime
import httpx
from typing import Optional, Dict
from database import save_exchange_rate

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

async def fetch_rate(source: str, target: str) -> float:
    """
    Fetch the latest exchange rate from Wise for source -> target.
    """
    url = f"https://wise.com/rates/history?source={source}&target={target}&length=1&resolution=hourly&unit=day"
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not data or not isinstance(data, list):
            raise ValueError(f"Unexpected response format from Wise: {data}")
        # Extract the rate value from the last element of the returned JSON array
        return float(data[-1]["value"])

async def fetch_and_save_rates(db_path: Optional[str] = None) -> Dict[str, float]:
    """
    Fetches JPY -> CNY and USD -> JPY rates, saves them to the database
    with current time as fetched_at, and returns a dictionary of the rates.
    """
    rate_jpy_cny = await fetch_rate("JPY", "CNY")
    rate_usd_jpy = await fetch_rate("USD", "JPY")
    
    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    
    await save_exchange_rate(db_path, "JPY", "CNY", rate_jpy_cny, fetched_at)
    await save_exchange_rate(db_path, "USD", "JPY", rate_usd_jpy, fetched_at)
    
    return {
        "JPY_CNY": rate_jpy_cny,
        "USD_JPY": rate_usd_jpy,
    }

def format_rates_message(rates: Dict[str, float]) -> str:
    """
    Formats a message with local time and rates (format: JPY_CNY: {:.4f}, USD_JPY: {:.2f}).
    """
    local_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    jpy_cny = rates.get("JPY_CNY", 0.0)
    usd_jpy = rates.get("USD_JPY", 0.0)
    return f"Exchange rates at {local_time}:\nJPY_CNY: {jpy_cny:.4f}\nUSD_JPY: {usd_jpy:.2f}"
