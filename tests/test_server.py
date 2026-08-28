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
        self.assertEqual(server.hyp_symbol("CRCLX"), "CRCL")
        self.assertEqual(server.hyp_symbol("SPCX"), "SPCX")
        self.assertEqual(server.hyp_symbol("crcl"), "CRCL")

    def test_raydium_position_pda_from_nft(self):
        self.assertEqual(
            server.raydium_position_pda("6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"),
            "AJWBXiEjp7GMokcurVK4uknufBHqoVnESbrrmwCmNH2p",
        )

    def test_raydium_position_is_decoded_on_chain(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        pool = "GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV"
        mint_a = "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1"
        mint_b = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        position_data = bytearray(300)
        position_data[9:41] = server.base58_decode(nft)
        position_data[41:73] = server.base58_decode(pool)
        position_data[73:77] = (44480).to_bytes(4, "little", signed=True)
        position_data[77:81] = (46480).to_bytes(4, "little", signed=True)
        position_data[81:97] = (10**12).to_bytes(16, "little")
        pool_data = bytearray(300)
        pool_data[73:105] = server.base58_decode(mint_a)
        pool_data[105:137] = server.base58_decode(mint_b)
        pool_data[233], pool_data[234] = 6, 6
        pool_data[253:269] = int((95**0.5) * 2**64).to_bytes(16, "little")
        with patch.object(server, "solana_account", side_effect=[bytes(position_data), bytes(pool_data)]):
            result = server.raydium_position(nft)
        self.assertEqual(result["pair"], "CRCLX / USDC")
        self.assertEqual(result["hedgeSymbol"], "CRCL")
        self.assertTrue(result["basisWarning"])
        self.assertTrue(result["importable"])

    def test_raydium_position_rejects_empty_liquidity(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        position_data = bytearray(300)
        position_data[9:41] = server.base58_decode(nft)
        position_data[41:73] = server.base58_decode("GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV")
        position_data[73:77] = (44480).to_bytes(4, "little", signed=True)
        position_data[77:81] = (46480).to_bytes(4, "little", signed=True)
        with patch.object(server, "solana_account", return_value=bytes(position_data)):
            with self.assertRaisesRegex(server.NeutralisError, "sem liquidez"):
                server.raydium_position(nft)

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
