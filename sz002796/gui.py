"""Compatibility helpers for the retired desktop GUI.

The production interface is now the aiohttp + React workbench.  ``run_gui.py``
remains as a compatibility entry point and launches the same web application.
``TickChartBuffer`` is retained as a small reusable/tested geometry helper.
"""
from __future__ import annotations

from datetime import datetime


class TickChartBuffer:
    def __init__(self, max_points: int = 300):
        self.max_points = max(2, int(max_points))
        self.points: list[tuple[str, float]] = []

    def append(self, tick: dict) -> None:
        price = float(tick.get("price", tick.get("Close", 0.0)) or 0.0)
        if price <= 0:
            return
        label = str(tick.get("server_time") or "")
        if not label:
            value = tick.get("Time") or tick.get("dt")
            label = value.strftime("%H:%M:%S") if isinstance(value, datetime) else str(value or "")
        self.points.append((label, price))
        if len(self.points) > self.max_points:
            self.points = self.points[-self.max_points :]

    def price_bounds(self) -> tuple[float, float]:
        if not self.points:
            return 0.0, 1.0
        prices = [price for _, price in self.points]
        low = min(prices)
        high = max(prices)
        if abs(high - low) < 1e-9:
            pad = max(abs(high) * 0.001, 0.01)
            return low - pad, high + pad
        pad = max((high - low) * 0.08, 0.01)
        return low - pad, high + pad

    def scaled_points(self, width: int, height: int, pad: int = 24) -> list[tuple[float, float]]:
        if not self.points:
            return []
        width = max(int(width), pad * 2 + 1)
        height = max(int(height), pad * 2 + 1)
        low, high = self.price_bounds()
        span = high - low if high > low else 1.0
        plot_w = max(width - pad * 2, 1)
        plot_h = max(height - pad * 2, 1)
        count = len(self.points)
        return [
            (
                pad + (plot_w if count == 1 else plot_w * index / (count - 1)),
                pad + (high - price) / span * plot_h,
            )
            for index, (_, price) in enumerate(self.points)
        ]


def main() -> None:
    from run_web import main as web_main

    web_main()
