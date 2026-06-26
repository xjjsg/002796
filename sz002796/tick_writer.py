"""Realtime tick CSV writer.

Each trading day is written to a separate 30-column CSV file. When the runtime
restarts, the writer reads the existing file's latest server_time and skips
stale or duplicate snapshots instead of appending old data again.
"""
import os
import csv
import time
from datetime import datetime

class TickDataWriter:
    def __init__(self, data_dir: str, symbol: str):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.symbol = symbol
        self.current_date_str = ""
        self.file = None
        self.csv_writer = None
        self.last_written_server_time = ""
        
    def _get_filename(self, date_str: str) -> str:
        return os.path.join(self.data_dir, f"{self.symbol}-{date_str}.csv")

    @staticmethod
    def _latest_server_time(filepath: str) -> str:
        latest = ""
        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    server_time = str(row.get("server_time", "") or "")
                    if server_time > latest:
                        latest = server_time
        except (OSError, csv.Error):
            return ""
        return latest
        
    def write(self, tick: dict, signal: str = "HOLD"):
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        
        if self.current_date_str != date_str:
            if self.file:
                self.file.close()
            self.current_date_str = date_str
            filepath = self._get_filename(date_str)
            file_exists = os.path.exists(filepath)
            self.last_written_server_time = self._latest_server_time(filepath) if file_exists else ""
            self.file = open(filepath, 'a', newline='', encoding='utf-8')
            
            self.header = [
                "local_time_ms", "server_time", "price", "open", "high", "low", "prev_close",
                "cum_volume", "cum_amount", "bp1", "bv1", "bp2", "bv2", "bp3", "bv3", "bp4", "bv4", "bp5", "bv5",
                "sp1", "sv1", "sp2", "sv2", "sp3", "sv3", "sp4", "sv4", "sp5", "sv5", "signal"
            ]
            self.csv_writer = csv.DictWriter(self.file, fieldnames=self.header, extrasaction='ignore')
            if not file_exists:
                self.csv_writer.writeheader()

        server_time = str(tick.get("server_time", "") or "")
        if server_time and self.last_written_server_time and server_time <= self.last_written_server_time:
            return False
                
        row = {
            "local_time_ms": int(time.time() * 1000),
            "server_time": server_time,
            "price": tick.get("price", ""),
            "open": tick.get("open", ""),
            "high": tick.get("high", ""),
            "low": tick.get("low", ""),
            "prev_close": tick.get("prev_close", ""),
            "cum_volume": tick.get("cum_volume", ""),
            "cum_amount": tick.get("cum_amount", ""),
            "signal": signal
        }
        
        for k in ["bp1", "bv1", "bp2", "bv2", "bp3", "bv3", "bp4", "bv4", "bp5", "bv5",
                  "sp1", "sv1", "sp2", "sv2", "sp3", "sv3", "sp4", "sv4", "sp5", "sv5"]:
            row[k] = tick.get(k, "")
            
        self.csv_writer.writerow(row)
        self.file.flush()
        if server_time:
            self.last_written_server_time = server_time
        return True
