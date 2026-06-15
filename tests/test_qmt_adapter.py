import unittest

from qmt.adapter import QmtTickNormalizer
from sz002796.strategy_v6 import CombinedStrategyV6


class QmtAdapterTests(unittest.TestCase):
    def test_qmt_tick_normalizer_maps_fields_and_deltas(self):
        normalizer = QmtTickNormalizer()
        first = normalizer.normalize(
            {
                "time": 20260601093003000,
                "lastPrice": 10.2,
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "lastClose": 9.8,
                "volume": 1000,
                "amount": 1020000,
                "askPrice1": 10.21,
                "askVol1": 300,
                "bidPrice1": 10.19,
                "bidVol1": 500,
            }
        )
        second = normalizer.normalize(
            {
                "time": 20260601093006000,
                "lastPrice": 10.25,
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "lastClose": 9.8,
                "volume": 1200,
                "amount": 1225000,
                "askPrice1": 10.26,
                "askVol1": 200,
                "bidPrice1": 10.24,
                "bidVol1": 400,
            }
        )

        self.assertEqual(first["Time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-06-01 09:30:03")
        self.assertEqual(first["price"], 10.2)
        self.assertEqual(first["Close"], 10.2)
        self.assertEqual(first["prev_close"], 9.8)
        self.assertEqual(first["sp1"], 10.21)
        self.assertEqual(first["bp1"], 10.19)
        self.assertEqual(first["sv1"], 30000)
        self.assertEqual(first["bv1"], 50000)
        self.assertEqual(first["qmt_raw_volume"], 1000)
        self.assertEqual(first["Volume"], 100000)
        self.assertEqual(first["tick_vol"], 100000)
        self.assertEqual(second["tick_vol"], 20000)
        self.assertEqual(second["tick_amt"], 205000)

    def test_qmt_realtime_list_orderbook_fields_are_mapped(self):
        tick = QmtTickNormalizer().normalize(
            {
                "time": 1780981200000,
                "lastPrice": 41.66,
                "open": 41.99,
                "high": 41.99,
                "low": 40.93,
                "lastClose": 41.36,
                "volume": 55660,
                "amount": 230911779.0,
                "askPrice": [41.66, 41.68, 41.69, 41.70, 41.71],
                "bidPrice": [41.60, 41.58, 41.56, 41.55, 41.54],
                "askVol": [2, 12, 8, 59, 1],
                "bidVol": [30, 2, 4, 13, 61],
            }
        )

        self.assertEqual(tick["Time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-06-09 13:00:00")
        self.assertEqual(tick["sp1"], 41.66)
        self.assertEqual(tick["bp1"], 41.60)
        self.assertEqual(tick["sp5"], 41.71)
        self.assertEqual(tick["bp5"], 41.54)
        self.assertEqual(tick["sv1"], 200)
        self.assertEqual(tick["bv1"], 3000)
        self.assertEqual(tick["sv4"], 5900)
        self.assertEqual(tick["bv5"], 6100)

    def test_normalized_tick_is_consumed_by_v6_strategy(self):
        tick = QmtTickNormalizer().normalize(
            {
                "time": 20260601100000000,
                "lastPrice": 10.2,
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "lastClose": 9.8,
                "volume": 1000,
                "amount": 1020000,
            }
        )
        strategy = CombinedStrategyV6()

        record = strategy.on_tick(tick)

        self.assertIsNone(record)
        self.assertIsNotNone(strategy.factor_calc.last_snapshot)
        self.assertEqual(strategy.factor_calc.last_snapshot.price, 10.2)


if __name__ == "__main__":
    unittest.main()
