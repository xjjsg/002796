"""Tencent realtime quote fetcher.

The fetcher converts the Tencent quote payload into the normalized tick shape
used by the strategy and writer. It also drops server timestamps that are not
strictly newer, which protects live collection from stale API snapshots.
"""
import asyncio
from datetime import datetime

class TencentFetcher:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.url = f"http://qt.gtimg.cn/q={symbol}"
        self.last_server_ts = None
        
    async def fetch(self, session) -> dict:
        try:
            async with session.get(self.url, timeout=5) as resp:
                text = await resp.text()
                if not text or len(text) < 50:
                    return None
                    
                parts = text.split("~")
                if len(parts) < 40:
                    return None
                    
                server_time_str = parts[30]
                if self.last_server_ts is not None and server_time_str <= self.last_server_ts:
                    return None
                self.last_server_ts = server_time_str
                
                dt = datetime.strptime(server_time_str, "%Y%m%d%H%M%S")
                
                tick = {
                    "Time": dt,
                    "server_time": dt.strftime("%H:%M:%S"),
                    "price": float(parts[3]),
                    "prev_close": float(parts[4]),
                    "open": float(parts[5]),
                    "cum_volume": float(parts[6]) * 100,
                    "cum_amount": float(parts[37]) * 10000,
                    "high": float(parts[33]),
                    "low": float(parts[34]),
                }
                
                for i in range(5):
                    tick[f"bp{i+1}"] = float(parts[9 + i*2])
                    tick[f"bv{i+1}"] = int(parts[10 + i*2]) * 100
                    tick[f"sp{i+1}"] = float(parts[19 + i*2])
                    tick[f"sv{i+1}"] = int(parts[20 + i*2]) * 100
                    
                tick["Close"] = tick["price"]
                tick["Volume"] = tick["cum_volume"]
                tick["Amount"] = tick["cum_amount"]
                
                return tick
        except Exception:
            return None
