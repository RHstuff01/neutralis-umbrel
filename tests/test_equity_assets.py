"""Regressões offline para os novos ativos em cada leitor de LP."""
import unittest
from decimal import Decimal
from unittest.mock import patch

from test_server import server


class EquityAssetTests(unittest.TestCase):
    CASES = (("NVDAX", "NVDA"), ("SPCXX", "SPCX"), ("SPACEXX", "SPCX"), ("GOOGLX", "GOOGL"))
    NFT = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
    POOL = "GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV"
    # Endereços sintéticos: a resolução de símbolos é simulada nestes testes.
    ASSET = "11111111111111111111111111111111"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def assert_position(self, result, symbol, hedge):
        self.assertEqual(result["assetSymbol"], symbol)
        self.assertEqual(result["hedgeSymbol"], hedge)
        self.assertTrue(result["importable"])
        self.assertGreater(result["liquidityUsd"], 0)
        self.assertLess(result["lowerPrice"], result["upperPrice"])

    def test_byreal_imports_both_token_orders(self):
        for symbol, hedge in self.CASES:
            for reverse in (False, True):
                with self.subTest(symbol=symbol, reverse=reverse):
                    tokens = [{"symbol": symbol, "decimals": 6}, {"symbol": "USDC", "decimals": 6}]
                    if reverse:
                        tokens.reverse()
                    pool = {"mintA": tokens[0], "mintB": tokens[1], "tickCurrent": 0}
                    position = {"lowerTick": -100, "upperTick": 100, "liquidityUsd": 1000}
                    self.assert_position(server.normalize_position(position, pool), symbol, hedge)

    def test_solana_readers_import_both_token_orders(self):
        for source in ("orca", "raydium"):
            for symbol, hedge in self.CASES:
                for reverse in (False, True):
                    with self.subTest(source=source, symbol=symbol, reverse=reverse):
                        mint_a, mint_b = (self.USDC, self.ASSET) if reverse else (self.ASSET, self.USDC)
                        position, pool = bytearray(300), bytearray(653)
                        if source == "orca":
                            position[:8] = server.ORCA_POSITION_DISCRIMINATOR
                            position[8:40] = server.base58_decode(self.POOL)
                            position[40:72] = server.base58_decode(self.NFT)
                            position[72:88] = (10**12).to_bytes(16, "little")
                            position[88:92] = (-100).to_bytes(4, "little", signed=True)
                            position[92:96] = (100).to_bytes(4, "little", signed=True)
                            pool[65:81] = (2**64).to_bytes(16, "little")
                            pool[101:133] = server.base58_decode(mint_a)
                            pool[181:213] = server.base58_decode(mint_b)
                            mint = bytearray(82)
                            mint[44] = 6
                            calls = [bytes(pool), bytes(mint), bytes(mint)]
                        else:
                            position[9:41] = server.base58_decode(self.NFT)
                            position[41:73] = server.base58_decode(self.POOL)
                            position[73:77] = (-100).to_bytes(4, "little", signed=True)
                            position[77:81] = (100).to_bytes(4, "little", signed=True)
                            position[81:97] = (10**12).to_bytes(16, "little")
                            pool[73:105] = server.base58_decode(mint_a)
                            pool[105:137] = server.base58_decode(mint_b)
                            pool[233] = pool[234] = 6
                            pool[253:269] = (2**64).to_bytes(16, "little")
                            calls = [bytes(position), bytes(pool)]
                        with patch.object(server, "solana_account", side_effect=calls), patch.object(
                            server, source + "_symbols", return_value={self.ASSET: symbol, self.USDC: "USDC"}
                        ):
                            result = server.orca_position(self.NFT, bytes(position)) if source == "orca" else server.raydium_position(self.NFT)
                        self.assert_position(result, symbol, hedge)

    def test_uniswap_decodes_aliases_and_keeps_original_symbol(self):
        for symbol, hedge in self.CASES:
            for reverse in (False, True):
                with self.subTest(symbol=symbol, reverse=reverse):
                    packed = ((-100 & 0xFFFFFF) << 8) | (100 << 32)
                    encoded = "".join(f"{v:064x}" for v in (1, 2, 3000, 60, 0, packed))
                    tokens = [(symbol, 6), ("USDG", 6)]
                    if reverse:
                        tokens.reverse()
                    with patch.object(server, "keccak", return_value=bytes.fromhex("ab" * 32)), patch.object(
                        server, "erc20_metadata", side_effect=tokens
                    ), patch.object(server, "robinhood_call", side_effect=[encoded, f"{2**96:064x}", f"{10**12:064x}"]):
                        result = server.uniswap_v4_position(42, "0x" + "ab" * 32)
                    self.assert_position(result, symbol, hedge)
                    self.assertEqual(result["pair"], symbol + " / USDG")
                    self.assertEqual(result["currentPrice"], Decimal(1))

    def test_uniswap_still_rejects_unapproved_asset(self):
        encoded = "".join(f"{v:064x}" for v in (1, 2, 3000, 60, 0, ((-100 & 0xFFFFFF) << 8) | (100 << 32)))
        with patch.object(server, "keccak", return_value=bytes.fromhex("ab" * 32)), patch.object(
            server, "erc20_metadata", side_effect=[("WNVDAX", 6), ("USDG", 6)]
        ), patch.object(server, "robinhood_call", return_value=encoded):
            self.assertIsNone(server.uniswap_v4_position(42, "0x" + "ab" * 32))
