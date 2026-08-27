import importlib.util
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["NEUTRALIS_DATA_DIR"] = TEST_DATA.name
SERVER_PATH = Path(__file__).resolve().parents[1] / "app" / "server.py"
SPEC = importlib.util.spec_from_file_location("neutralis_umbrel_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


class NeutralisTests(unittest.TestCase):
    def test_xstock_symbol_maps_to_hyperliquid(self):
        self.assertEqual(server.hyp_symbol("AAPLX"), "AAPL")
        self.assertEqual(server.hyp_symbol("crcl"), "CRCL")

    def test_byreal_position_normalization(self):
        position = {
            "positionAddress": "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw",
            "poolAddress": "pool",
            "lowerTick": 56000,
            "upperTick": 57000,
            "liquidityUsd": "1000",
        }
        pool = {
            "poolAddress": "pool",
            "mintA": {"symbol": "AAPLX", "decimals": 6},
            "mintB": {"symbol": "USDC", "decimals": 6},
        }
        result = server.normalize_position(position, pool)
        self.assertTrue(result["importable"])
        self.assertEqual(result["assetSymbol"], "AAPLX")
        self.assertEqual(result["hedgeSymbol"], "AAPL")
        self.assertLess(result["lowerPrice"], result["upperPrice"])

    def test_byreal_envelope_is_unwrapped(self):
        payload = {
            "result": {
                "data": {
                    "positions": [{"positionAddress": "position", "poolAddress": "pool", "lowerTick": -10, "upperTick": 10, "liquidityUsd": 100}],
                    "poolMap": {"pool": {"poolAddress": "pool", "mintA": {"symbol": "CRCL", "decimals": 6}, "mintB": {"symbol": "USDC", "decimals": 6}}},
                }
            }
        }
        with patch.object(server, "json_request", return_value=payload):
            positions = server.byreal_positions("6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["hedgeSymbol"], "CRCL")

    def test_clmm_target_reduces_as_price_rises(self):
        value = Decimal("1000")
        lower = Decimal("90")
        mark = Decimal("100")
        upper = Decimal("110")
        liquidity = server.lp_liquidity(value, mark, lower, upper)
        below = server.base_target(liquidity, Decimal("95"), lower, upper)
        center = server.base_target(liquidity, mark, lower, upper)
        above = server.base_target(liquidity, Decimal("105"), lower, upper)
        self.assertGreater(below, center)
        self.assertGreater(center, above)

    def test_service_contains_no_trading_endpoint(self):
        source = SERVER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/exchange", source)
        self.assertNotIn("private_key", source.lower())


if __name__ == "__main__":
    unittest.main()
