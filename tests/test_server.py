import importlib.util
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch


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
        self.assertEqual(server.hyp_symbol("COINX"), "COIN")
        self.assertEqual(server.hyp_symbol("SPCX"), "SPCX")
        self.assertEqual(server.hyp_symbol("SPYx"), "US500")
        self.assertEqual(server.hedge_mode("SPYx"), "units")
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

    def test_orca_position_is_decoded_from_official_layout(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        pool = "GYqHjuDzTiw7i52Xv1qohDE6eJr6eSZpsrBVikGZyaFV"
        mint_a = "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1"
        mint_b = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        position_data = bytearray(216)
        position_data[:8] = server.ORCA_POSITION_DISCRIMINATOR
        position_data[8:40] = server.base58_decode(pool)
        position_data[40:72] = server.base58_decode(nft)
        position_data[72:88] = (10**12).to_bytes(16, "little")
        position_data[88:92] = (44480).to_bytes(4, "little", signed=True)
        position_data[92:96] = (46480).to_bytes(4, "little", signed=True)
        pool_data = bytearray(653)
        pool_data[65:81] = int((95**0.5) * 2**64).to_bytes(16, "little")
        pool_data[101:133] = server.base58_decode(mint_a)
        pool_data[181:213] = server.base58_decode(mint_b)
        mint_a_data, mint_b_data = bytearray(82), bytearray(82)
        mint_a_data[44] = mint_b_data[44] = 6
        with patch.object(server, "solana_account", side_effect=[bytes(pool_data), bytes(mint_a_data), bytes(mint_b_data)]):
            result = server.orca_position(nft, bytes(position_data))
        self.assertEqual(result["source"], "orca")
        self.assertEqual(result["pair"], "CRCLX / USDC")
        self.assertEqual(result["hedgeSymbol"], "CRCL")
        self.assertTrue(result["importable"])

    def test_orca_spyx_usdc_uses_known_spyx_mint_without_token_api(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        pool = "Fae5dWVntUt6zbWu2voXxioDpMii7SqQwtsxBmoVCsHR"
        mint_a = "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W"
        mint_b = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        position_data = bytearray(216)
        position_data[:8] = server.ORCA_POSITION_DISCRIMINATOR
        position_data[8:40] = server.base58_decode(pool)
        position_data[40:72] = server.base58_decode(nft)
        position_data[72:88] = (10**12).to_bytes(16, "little")
        position_data[88:92] = (66000).to_bytes(4, "little", signed=True)
        position_data[92:96] = (68000).to_bytes(4, "little", signed=True)
        pool_data = bytearray(653)
        pool_data[65:81] = int((775**0.5) * 2**64).to_bytes(16, "little")
        pool_data[101:133] = server.base58_decode(mint_a)
        pool_data[181:213] = server.base58_decode(mint_b)
        mint_a_data, mint_b_data = bytearray(82), bytearray(82)
        mint_a_data[44] = mint_b_data[44] = 6
        with patch.object(server, "solana_account", side_effect=[bytes(pool_data), bytes(mint_a_data), bytes(mint_b_data)]):
            result = server.orca_position(nft, bytes(position_data))
        self.assertEqual(result["pair"], "SPYX / USDC")
        self.assertEqual(result["hedgeSymbol"], "US500")
        self.assertEqual(result["hedgeMode"], "units")
        self.assertTrue(result["importable"])

    def test_orca_zec_usdc_uses_known_zec_mint_without_token_api(self):
        zec = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertEqual(server.orca_symbols([zec, usdc]), {zec: "ZEC", usdc: "USDC"})
        self.assertEqual(server.hyp_symbol("ZEC"), "ZEC")

    def test_orca_discovers_classic_and_token_2022_position_nfts(self):
        wallet = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        classic = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        token_2022 = "CNJt5jfTNps9HxE6CRgefvFCTrNdYAcetJSEosaLHzq4"
        def payload(mint):
            account = server.base58_decode(mint) + bytes(32) + (1).to_bytes(8, "little") + bytes(93)
            return {"result": {"value": [{"account": {"data": [server.base64.b64encode(account).decode(), "base64"]}}]}}
        with patch.object(server, "json_request", side_effect=[payload(classic), payload(token_2022)]):
            self.assertEqual(server.solana_nft_mints(wallet), sorted([classic, token_2022]))

    def test_orca_bundle_pdas_match_official_sdk_vectors(self):
        mint = "6sf6fSK6tTubFA2LMCeTzt4c6DeNVyA6WpDDgtWs7a5p"
        bundle = server.orca_position_bundle_pda(mint)
        self.assertEqual(bundle, "At1QvbnANV6imkdNkfB4h1XsY4jbTzPAmScgjLCnM7jy")
        self.assertEqual(
            server.orca_bundled_position_pda(bundle, 0),
            "4GRbpiDX46zi2AdZ2b9Ho4zfpLXhpsYBhRzkp2AeZej3",
        )
        immutable_mint = "6LdmNS8p3qLYrGcPeYby6zHRvZPq7cYDZTiBXCC3FNDs"
        immutable_bundle = server.orca_position_bundle_pda(
            immutable_mint, server.ORCA_IMMUTABLE_WHIRLPOOL_PROGRAM
        )
        self.assertEqual(immutable_bundle, "CVTZ5u8yjGngtpZ5WRx536ty8jiMCFkzwrr5TJW5FpR7")
        self.assertEqual(
            server.orca_bundled_position_pda(
                immutable_bundle, 0, server.ORCA_IMMUTABLE_WHIRLPOOL_PROGRAM
            ),
            "FMAeLNU3RRb31UXJTmHcVwDYBJQwy7DhZepFk9Vwc1Mi",
        )

    def test_orca_discovers_positions_inside_bundle(self):
        wallet = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        mint = "6sf6fSK6tTubFA2LMCeTzt4c6DeNVyA6WpDDgtWs7a5p"
        bundle = server.orca_position_bundle_pda(mint)
        bundled_position = server.orca_bundled_position_pda(bundle, 0)
        bundle_data = server.ORCA_POSITION_BUNDLE_DISCRIMINATOR + server.base58_decode(mint) + bytes([1]) + bytes(31)
        position_data = server.ORCA_POSITION_DISCRIMINATOR + bytes(88)
        decoded = {"source": "orca", "positionAddress": mint, "poolAddress": "pool", "importable": True}
        with patch.object(server, "solana_nft_mints", return_value=[mint]), patch.object(
            server, "solana_accounts", side_effect=[
                {bundle: bundle_data},
                {bundled_position: position_data},
                {},
            ]
        ), patch.object(server, "orca_position", return_value=decoded):
            positions = server.orca_positions_manual(wallet)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["positionAddress"], bundled_position)
        self.assertEqual(positions[0]["positionNftMint"], mint)
        self.assertEqual(positions[0]["bundleIndex"], 0)

    def test_orca_ignores_non_nft_token_accounts(self):
        wallet = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        mint = "3obGz9gF9MTcvyebAofE1bS21fTA1sfV9KFBJMsfvfTK"
        empty = {"result": {"value": []}}
        account = server.base58_decode(mint) + bytes(32) + (2).to_bytes(8, "little") + bytes(93)
        token_2022 = {"result": {"value": [{"account": {"data": [server.base64.b64encode(account).decode(), "base64"]}}]}}
        with patch.object(server, "json_request", side_effect=[empty, token_2022]):
            self.assertEqual(server.solana_nft_mints(wallet), [])

    def test_orca_accepts_personal_position_account(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        position_address = server.orca_position_pda(nft)
        data = bytearray(216)
        data[:8] = server.ORCA_POSITION_DISCRIMINATOR
        data[40:72] = server.base58_decode(nft)
        decoded = {"positionAddress": nft, "personalPositionAddress": "derived"}
        with patch.object(server, "solana_account", return_value=bytes(data)), patch.object(
            server, "orca_position", return_value=decoded
        ) as decode:
            result = server.orca_position_from_address(position_address)
        decode.assert_called_once_with(nft, bytes(data), server.ORCA_WHIRLPOOL_PROGRAM)
        self.assertEqual(result["positionAddress"], nft)
        self.assertEqual(result["personalPositionAddress"], position_address)

    def test_orca_recognizes_whirlpool_address_for_wallet_filtering(self):
        pool = "C9U2Ksk6KKWvLEeo5yUQ7Xu46X7NzeBJtd9PBfuXaUSM"
        data = server.ORCA_WHIRLPOOL_DISCRIMINATOR + bytes(645)
        with patch.object(server, "solana_account", return_value=data):
            self.assertIsNone(server.orca_position_from_address(pool))

    def test_selected_orca_position_matches_personal_position_account(self):
        address = "3obGz9gF9MTcvyebAofE1bS21fTA1sfV9KFBJMsfvfTK"
        position = {
            "positionAddress": "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw",
            "personalPositionAddress": address,
            "importable": True,
        }
        original = server.MONITOR.config
        server.MONITOR.config = {**original, "positionAddress": address}
        try:
            with patch.object(server.MONITOR, "positions", return_value=[position]):
                self.assertIs(server.MONITOR._selected_position(), position)
        finally:
            server.MONITOR.config = original

    def test_selected_orca_position_matches_pool_address(self):
        pool = "C9U2Ksk6KKWvLEeo5yUQ7Xu46X7NzeBJtd9PBfuXaUSM"
        position = {"positionAddress": "nft", "personalPositionAddress": "position", "poolAddress": pool, "importable": True}
        original = server.MONITOR.config
        server.MONITOR.config = {**original, "positionAddress": pool}
        try:
            with patch.object(server.MONITOR, "positions", return_value=[position]):
                self.assertIs(server.MONITOR._selected_position(), position)
        finally:
            server.MONITOR.config = original

    def test_orca_stale_pool_does_not_hide_discovered_positions(self):
        pool = "C9U2Ksk6KKWvLEeo5yUQ7Xu46X7NzeBJtd9PBfuXaUSM"
        position = {"positionAddress": "nft", "poolAddress": "another-pool", "importable": True}
        original = server.MONITOR.config
        server.MONITOR.config = {**original, "source": "orca", "positionAddress": pool}
        try:
            with patch.object(server, "orca_position_from_address", return_value=None), patch.object(
                server, "orca_positions", return_value=[position]
            ):
                self.assertEqual(server.MONITOR.positions(), [position])
        finally:
            server.MONITOR.config = original

    def test_selected_orca_position_survives_wallet_discovery_failure(self):
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        position = {"positionAddress": nft, "importable": True}
        original = server.MONITOR.config
        server.MONITOR.config = {**original, "source": "orca", "positionAddress": nft}
        try:
            with patch.object(server, "orca_position_from_address", return_value=position), patch.object(
                server, "orca_positions", side_effect=server.NeutralisError("RPC temporariamente indisponível")
            ):
                self.assertEqual(server.MONITOR.positions(), [position])
        finally:
            server.MONITOR.config = original

    def test_initialization_prepares_persistent_data(self):
        with patch.object(server.os, "chmod") as chmod:
            server.prepare_data_permissions()
        chmod.assert_any_call(server.DATA_DIR, 0o700)

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

    def test_byreal_current_tick_provides_independent_lp_price(self):
        position = {"positionAddress": "position", "poolAddress": "pool", "lowerTick": 51000, "upperTick": 53000, "liquidityUsd": "1000"}
        pool = {
            "poolAddress": "pool",
            "tickCurrent": 52000,
            "mintA": {"symbol": "COINX", "decimals": 6},
            "mintB": {"symbol": "USDC", "decimals": 6},
        }
        result = server.normalize_position(position, pool)
        self.assertAlmostEqual(result["currentPrice"], 1.0001**52000)

    def test_missing_byreal_price_can_fall_back_to_mark_in_dry_run(self):
        position = {"currentPrice": None}
        mark = Decimal("176.79")
        self.assertEqual(server.decimal(position.get("currentPrice") or mark, "preço da LP"), mark)

    def test_byreal_mint_price_requires_exact_mint(self):
        mint = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        payload = {"result": {"data": {"records": [
            {"mintAddress": "11111111111111111111111111111111", "priceUsd": "999"},
            {"mintAddress": mint, "priceUsd": "177.73"},
        ]}}}
        with patch.object(server, "json_request", return_value=payload):
            self.assertEqual(server.byreal_mint_price(mint), 177.73)

    def test_byreal_positions_enriches_price_from_mint_catalog(self):
        wallet = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        asset_mint = "CNJt5jfTNps9HxE6CRgefvFCTrNdYAcetJSEosaLHzq4"
        position_payload = {"result": {"data": {
            "positions": [{"positionAddress": "position", "poolAddress": "pool", "lowerTick": 51000, "upperTick": 53000, "liquidityUsd": "1000"}],
            "poolMap": {"pool": {"poolAddress": "pool", "mintA": {"address": asset_mint, "symbol": "COINX", "decimals": 6}, "mintB": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "symbol": "USDC", "decimals": 6}}},
        }}}
        price_payload = {"result": {"data": {"records": [{"mintAddress": asset_mint, "priceUsd": "177.73"}]}}}
        with patch.object(server, "json_request", side_effect=[position_payload, price_payload]):
            positions = server.byreal_positions(wallet)
        self.assertEqual(positions[0]["currentPrice"], 177.73)

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

    def test_adaptive_rebalance_step_uses_lp_range_width(self):
        self.assertEqual(server.adaptive_rebalance_step(Decimal("99"), Decimal("101")), Decimal("0.0025"))
        self.assertEqual(server.adaptive_rebalance_step(Decimal("98.5"), Decimal("101.5")), Decimal("0.005"))

    def test_spyx_target_uses_same_units_as_mkts_us500(self):
        position = {
            "positionAddress": "position", "assetSymbol": "SPYX", "hedgeSymbol": "US500",
            "hedgeMode": "units", "liquidityUsd": Decimal("10000"),
            "normalizedLiquidity": Decimal("1000"), "currentPrice": Decimal("775"),
            "lowerPrice": Decimal("700"), "upperPrice": Decimal("850"), "importable": True,
        }
        hyp = server.HypState("mkts:US500", 3, Decimal("775"), Decimal("775"), Decimal("0"), 0, "mkts")
        asset_delta = server.base_target(Decimal("1000"), Decimal("775"), Decimal("700"), Decimal("850"))
        with patch.object(server.MONITOR, "_selected_position", return_value=position), patch.object(
            server, "hyp_state", return_value=hyp
        ):
            *_, target = server.MONITOR._live_snapshot()
        self.assertEqual(target, asset_delta)

    def test_hyp_price_projection_changes_spyx_target_without_orca_tick(self):
        position = {"hedgeMode": "units"}
        liquidity = Decimal("1000")
        lower, upper = Decimal("700"), Decimal("850")
        at_anchor = server.target_at_reference_price(position, liquidity, Decimal("775"), lower, upper, Decimal("775"))
        # Mesmo se o tick da Orca ainda não mudou, +0,5% no US500 deve
        # diminuir a exposição estimada de SPYx e habilitar o rebalanceamento.
        after_hyp_move = server.target_at_reference_price(position, liquidity, Decimal("778.875"), lower, upper, Decimal("778.875"))
        self.assertLess(after_hyp_move, at_anchor)

    def test_snapshot_keeps_hedge_when_lp_is_outside_active_range(self):
        position = {
            "positionAddress": "position", "assetSymbol": "SPYX", "hedgeSymbol": "US500",
            "hedgeMode": "units", "liquidityUsd": Decimal("10000"),
            "normalizedLiquidity": Decimal("1000"), "currentPrice": Decimal("851"),
            "lowerPrice": Decimal("700"), "upperPrice": Decimal("850"), "importable": True,
        }
        hyp = server.HypState("mkts:US500", 3, Decimal("775"), Decimal("775"), Decimal("0"), 0, "mkts")
        with patch.object(server.MONITOR, "_selected_position", return_value=position), patch.object(
            server, "hyp_state", return_value=hyp
        ):
            *_, target = server.MONITOR._live_snapshot()
        # Acima da faixa, a LP fica integralmente em USDC e o short-alvo é 0.
        self.assertEqual(target, Decimal("0"))

    def test_liquidity_can_be_inferred_below_or_above_range(self):
        lower, upper = Decimal("90"), Decimal("110")
        self.assertGreater(server.lp_liquidity(Decimal("1000"), Decimal("80"), lower, upper), 0)
        self.assertGreater(server.lp_liquidity(Decimal("1000"), Decimal("120"), lower, upper), 0)

    def test_upper_exit_finishes_monitor_after_short_is_zero(self):
        server.MONITOR.stop_event.clear()
        with patch.object(server.MONITOR, "_event") as event:
            server.MONITOR._finish_upper_exit("xyz:AAPL", Decimal("200"), live=True)
        self.assertEqual(server.MONITOR.state["mode"], "stopped")
        self.assertIn("short zerado", server.MONITOR.state["message"])
        event.assert_called_once()
        server.MONITOR.stop_event.clear()

    def test_spyx_maps_to_units_contract_on_mkts_dex(self):
        self.assertEqual(server.hyp_symbol("SPYX"), "US500")
        self.assertEqual(server.hyp_dex("US500"), "mkts")
        self.assertEqual(server.hedge_mode("SPYX"), "units")

    def test_zec_and_sol_use_main_hyperliquid_market(self):
        self.assertIsNone(server.hyp_dex("ZEC"))
        self.assertIsNone(server.hyp_dex("SOL"))

    def test_main_hyperliquid_market_is_queried_without_dex(self):
        account = "0x622dF631Bb769123FC7b8FEd0d2C363045aceDCF"
        metadata = {"universe": [{"name": "ZEC", "szDecimals": 3}]}
        contexts = [{"markPx": "42", "oraclePx": "42"}]
        clearinghouse = {"assetPositions": [{"position": {"coin": "ZEC", "szi": "-1.2"}}]}
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), clearinghouse, []]) as request:
            hyp = server.hyp_state(account, "ZEC")
        self.assertEqual(hyp.market, "ZEC")
        self.assertEqual(hyp.signed_position, Decimal("-1.2"))
        self.assertTrue(all("dex" not in call.args[1] for call in request.call_args_list))

    def test_basis_guard_measures_drift_from_anchor_not_initial_spread(self):
        position = {"hedgeMode": "units"}
        anchor_ratio = Decimal("770.83") / Decimal("766.31")
        self.assertEqual(server.hedge_basis(position, Decimal("770.83"), Decimal("766.31"), anchor_ratio), 0)
        self.assertLess(
            server.hedge_basis(position, Decimal("774.68"), Decimal("766.31"), anchor_ratio),
            Decimal("0.0051"),
        )

    def test_live_activation_accepts_simple_confirmation(self):
        position = {"assetSymbol": "COINX", "hedgeSymbol": "COIN"}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("0"), 0)
        api_key_file = Mock()
        api_key_file.exists.return_value = True
        with patch.object(server.MONITOR, "_live_snapshot", return_value=(position, hyp, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"))), patch.object(server, "API_KEY_FILE", api_key_file), patch.object(server.threading, "Thread") as thread:
            server.MONITOR.start(live=True, confirmation="ATIVAR")
        thread.assert_called_once()

    def test_api_wallet_key_is_never_returned(self):
        key = "11" * 32
        result = server.MONITOR.save_api_key({"privateKey": key})
        self.assertEqual(result, {"configured": True})
        self.assertNotIn(key, str(server.MONITOR.public_state()))

    def test_api_wallet_rejects_invalid_key(self):
        with self.assertRaisesRegex(server.NeutralisError, "inválida"):
            server.MONITOR.save_api_key({"privateKey": "segredo"})

    def test_auto_adjustment_waits_below_hyperliquid_minimum(self):
        position = {"hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        with patch.object(server.MONITOR, "_exchange") as exchange:
            result = server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("2.749"))
        self.assertIsNone(result)
        exchange.assert_not_called()

    def test_auto_buy_is_reduce_only_and_never_crosses_zero(self):
        position = {"positionAddress": "position", "hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        completed = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.600"), 0)
        snapshot = (position, completed, Decimal("150"), Decimal("200"), Decimal("1"), Decimal("2.600"))
        response = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.140"}}]}}}
        exchange = Mock()
        exchange.order.return_value = response
        with patch.object(server, "hyp_state", return_value=hyp), patch.object(server.MONITOR, "_live_snapshot", return_value=snapshot), patch.object(server.MONITOR, "_exchange", return_value=exchange), patch.object(server.MONITOR, "_event"), patch.object(server.MONITOR.stop_event, "wait", return_value=False):
            result = server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("2.600"))
        self.assertTrue(result["isBuy"])
        self.assertEqual(exchange.order.call_args.kwargs["reduce_only"], True)
        self.assertEqual(exchange.order.call_args.args[2], 0.14)
        self.assertLess(result["residualNotional"], server.AUTO_MIN_ORDER_NOTIONAL)

    def test_partial_ioc_is_recalculated_and_retried(self):
        position = {"positionAddress": "position", "hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        initial = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        partial = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.670"), 0)
        completed = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.600"), 0)
        snapshots = [
            (position, partial, Decimal("150"), Decimal("200"), Decimal("1"), Decimal("2.600")),
            (position, completed, Decimal("150"), Decimal("200"), Decimal("1"), Decimal("2.600")),
        ]
        responses = [
            {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.070"}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.070"}}]}}},
        ]
        exchange = Mock()
        exchange.order.side_effect = responses
        with patch.object(server, "hyp_state", side_effect=[initial, partial]), patch.object(server.MONITOR, "_live_snapshot", side_effect=snapshots), patch.object(server.MONITOR, "_exchange", return_value=exchange), patch.object(server.MONITOR, "_event"), patch.object(server.MONITOR.stop_event, "wait", return_value=False):
            result = server.MONITOR._execute_auto_adjustment(position, initial, Decimal("2.600"))
        self.assertEqual(exchange.order.call_count, 2)
        self.assertEqual(result["currentShort"], Decimal("2.600"))
        self.assertEqual(result["filled"], Decimal("0.140"))

    def test_unfilled_ioc_retries_three_times_then_pauses(self):
        position = {"positionAddress": "position", "hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        snapshot = (position, hyp, Decimal("150"), Decimal("200"), Decimal("1"), Decimal("2.600"))
        response = {"status": "ok", "response": {"data": {"statuses": [{"error": "IocCancel"}]}}}
        exchange = Mock()
        exchange.order.return_value = response
        with patch.object(server, "hyp_state", return_value=hyp), patch.object(server.MONITOR, "_live_snapshot", return_value=snapshot), patch.object(server.MONITOR, "_exchange", return_value=exchange), patch.object(server.MONITOR, "_event"), patch.object(server.MONITOR.stop_event, "wait", return_value=False):
            with self.assertRaisesRegex(server.NeutralisError, "Hedge incompleto após 3 tentativas"):
                server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("2.600"))
        self.assertEqual(exchange.order.call_count, 3)

    def test_auto_sell_pauses_above_six_hundred_total_notional(self):
        position = {"hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        with self.assertRaisesRegex(server.NeutralisError, "US\$ 600"):
            server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("3.500"))

    def test_configured_notional_limit_is_used(self):
        original = dict(server.MONITOR.config)
        try:
            server.MONITOR.config = {**original, "maxPositionNotional": "12000"}
            self.assertEqual(server.MONITOR.max_position_notional(), Decimal("12000"))
            self.assertEqual(server.MONITOR.public_state()["autoLimits"]["maxPositionNotional"], 12000.0)
        finally:
            server.MONITOR.config = original

    def test_ioc_without_fill_is_rejected(self):
        response = {"status": "ok", "response": {"data": {"statuses": [{"error": "IocCancel"}]}}}
        with self.assertRaisesRegex(server.NeutralisError, "IocCancel"):
            server.MONITOR._order_status(response)

    def test_monitor_start_uses_anchor_without_initial_adjustment(self):
        position = {
            "positionAddress": "position",
            "hedgeSymbol": "COIN",
            "currentPrice": Decimal("176"),
        }
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        snapshot = (position, hyp, Decimal("159"), Decimal("195"), Decimal("755"), Decimal("2.749"))
        events = []
        with patch.object(server.MONITOR, "_live_snapshot", return_value=snapshot), patch.object(server.MONITOR.stop_event, "wait", return_value=True), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
            server.MONITOR._run(live=False)
        self.assertEqual(events, ["start"])
        self.assertEqual(server.MONITOR.state["snapshot"]["anchor"], 176.0)
        self.assertEqual(server.MONITOR.state["snapshot"]["virtualShort"], 2.74)

    def test_live_monitor_reconciles_initial_delta_before_setting_anchor(self):
        position = {
            "positionAddress": "position",
            "assetSymbol": "COINX",
            "hedgeSymbol": "COIN",
            "currentPrice": Decimal("176"),
        }
        initial = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        corrected = server.HypState("xyz:COIN", 3, Decimal("176.1"), Decimal("176.1"), Decimal("-2.600"), 0)
        before = (position, initial, Decimal("159"), Decimal("195"), Decimal("755"), Decimal("2.600"))
        after = (position, corrected, Decimal("159"), Decimal("195"), Decimal("755"), Decimal("2.600"))
        result = {"currentShort": Decimal("2.600"), "anchor": Decimal("176.1")}
        events = []
        with patch.object(server.MONITOR, "_live_snapshot", side_effect=[before, after]), patch.object(server.MONITOR, "_execute_auto_adjustment", return_value=result) as adjustment, patch.object(server.MONITOR.stop_event, "wait", return_value=True), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
            server.MONITOR._run(live=True)
        adjustment.assert_called_once_with(position, initial, Decimal("2.600"))
        self.assertEqual(events, ["initial-reconciliation", "start-live"])
        self.assertEqual(server.MONITOR.state["snapshot"]["anchor"], 176.0)
        self.assertEqual(server.MONITOR.state["snapshot"]["realShort"], 2.6)


if __name__ == "__main__":
    unittest.main()
