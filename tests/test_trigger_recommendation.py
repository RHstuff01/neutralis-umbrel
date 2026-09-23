import unittest

from app.trigger_recommendation import TriggerRecommendationError, build_recommendation, fetch_candles


def candles(prices, start=1_700_000_000_000):
    return [{"t": start + i * 60_000, "h": str(p * 1.0005), "l": str(p * 0.9995), "c": str(p)} for i, p in enumerate(prices)]


CONTEXT = {
    "valueUsd": 10_000,
    "lower": 90,
    "upper": 110,
    "liquidity": 105_000,
    "lpPrice": 100,
    "hypMark": 100,
}


class TriggerRecommendationTests(unittest.TestCase):
    def test_requires_selected_lp_context(self):
        with self.assertRaisesRegex(TriggerRecommendationError, "Selecione e salve uma LP"):
            build_recommendation(candles([100] * 600), 1.0)

    def test_returns_three_economic_profiles_using_real_lp_value(self):
        prices = [100 + ((i % 120) - 60) * 0.03 for i in range(3_000)]
        result = build_recommendation(candles(prices), 1.25, CONTEXT)
        self.assertEqual(result["lpValueUsd"], 10_000)
        self.assertEqual(result["currentPercent"], 1.25)
        self.assertEqual([p["name"] for p in result["profiles"]], ["Mais protegido", "Equilibrado", "Menos operações"])
        self.assertIn(result["recommendedPercent"], {0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0})
        self.assertIn(result["recommendedHoldSeconds"], {60, 120, 180, 300, 600})
        self.assertTrue(all("combinedResultUsd" in profile for profile in result["profiles"]))

    def test_invalid_lp_economics_are_rejected(self):
        invalid = {**CONTEXT, "valueUsd": 0}
        with self.assertRaisesRegex(TriggerRecommendationError, "inválidos"):
            build_recommendation(candles([100] * 600), 1.0, invalid)

    def test_short_history_is_rejected_after_lp_is_validated(self):
        with self.assertRaisesRegex(TriggerRecommendationError, "300 velas"):
            build_recommendation(candles([100] * 299), 1.0, CONTEXT)

    def test_fetches_30_day_history_in_chunks_and_deduplicates(self):
        calls = []
        def requester(_url, payload):
            calls.append(payload)
            start = payload["req"]["startTime"]
            return [{"t": start, "h": "100", "l": "99", "c": "99.5"}]
        result = fetch_candles("xyz:SPCX", requester, "https://example.invalid", days=30, now_ms=1_700_000_000_000)
        self.assertGreater(len(calls), 7)
        self.assertEqual(len(result), len(calls))
        self.assertTrue(all(call["req"]["coin"] == "xyz:SPCX" for call in calls))


if __name__ == "__main__":
    unittest.main()
