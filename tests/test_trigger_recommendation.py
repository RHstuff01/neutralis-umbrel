import unittest

from app.trigger_recommendation import TriggerRecommendationError, build_recommendation, fetch_candles, recommend_hold_seconds


def candles(prices, start=1_700_000_000_000):
    return [
        {"t": start + index * 60_000, "h": str(price * 1.0005), "l": str(price * 0.9995), "c": str(price)}
        for index, price in enumerate(prices)
    ]


class TriggerRecommendationTests(unittest.TestCase):
    def test_volatile_history_recommends_larger_trigger(self):
        calm = candles([100 + ((index % 20) - 10) * 0.01 for index in range(600)])
        volatile = candles([100 + ((index % 20) - 10) * 0.20 for index in range(600)])
        calm_result = build_recommendation(calm, 1.0)
        volatile_result = build_recommendation(volatile, 1.0)
        self.assertGreater(volatile_result["recommendedPercent"], calm_result["recommendedPercent"])
        self.assertGreaterEqual(calm_result["recommendedPercent"], 0.5)

    def test_recommendation_is_capped_and_does_not_mutate_current_value(self):
        result = build_recommendation(candles([100 if index % 2 else 90 for index in range(600)]), 1.25)
        self.assertEqual(result["recommendedPercent"], 3.0)
        self.assertEqual(result["currentPercent"], 1.25)
        self.assertIn(result["recommendedHoldSeconds"], {60, 120, 180, 300, 600})

    def test_hold_recommendation_uses_minute_resolution(self):
        start = 1_700_000_000_000
        prices = [100, 100, 99, 99, 99.95, 100, 99, 99, 99, 99.95]
        normalized = [(start + index * 60_000, price, price) for index, price in enumerate(prices)]
        hold, samples = recommend_hold_seconds(normalized, 1.0)
        self.assertGreaterEqual(hold, 60)
        self.assertEqual(samples, 2)

    def test_short_history_is_rejected(self):
        with self.assertRaisesRegex(TriggerRecommendationError, "300 velas"):
            build_recommendation(candles([100] * 299), 1.0)

    def test_fetches_history_in_chunks_and_deduplicates(self):
        calls = []
        def requester(_url, payload):
            calls.append(payload)
            start = payload["req"]["startTime"]
            return [{"t": start, "h": "100", "l": "99"}]
        result = fetch_candles("xyz:SPCX", requester, "https://example.invalid", days=7, now_ms=1_700_000_000_000)
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(result), len(calls))
        self.assertTrue(all(call["req"]["coin"] == "xyz:SPCX" for call in calls))


if __name__ == "__main__":
    unittest.main()
