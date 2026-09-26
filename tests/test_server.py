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
    def test_new_installation_does_not_ship_with_wallet_addresses(self):
        monitor = server.NeutralisMonitor("blank-wallet-defaults")

        self.assertEqual(monitor.config["solanaWallet"], "")
        self.assertEqual(monitor.config["evmWallet"], "")
        self.assertEqual(monitor.config["hyperliquidAccount"], "")

    def setUp(self):
        for monitor in server.MONITORS.values():
            monitor.persisted_strategy = None
            monitor.performance_state = None
            monitor.performance_cache = {}
            try:
                monitor.strategy_state_file.unlink()
            except FileNotFoundError:
                pass
            try:
                monitor.performance_state_file.unlink()
            except FileNotFoundError:
                pass

    def test_hyp_performance_sums_only_selected_market(self):
        responses = [
            [
                {"coin": "xyz:IBM", "closedPnl": "12.50", "fee": "0.30"},
                {"coin": "xyz:AAPL", "closedPnl": "99", "fee": "9"},
                {"coin": "IBM", "closedPnl": "-2.00", "fee": "0.20"},
            ],
            [
                {"delta": {"coin": "xyz:IBM", "usdc": "1.25"}},
                {"delta": {"coin": "AAPL", "usdc": "5"}},
            ],
        ]
        with patch.object(server, "json_request", side_effect=responses):
            result = server.hyp_performance_since("0x1111111111111111111111111111111111111111", "xyz:IBM", 1_700_000_000_000)
        self.assertEqual(result["realizedPnlUsd"], Decimal("10.50"))
        self.assertEqual(result["feesUsd"], Decimal("0.50"))
        self.assertEqual(result["fundingUsd"], Decimal("1.25"))

    def test_real_result_baseline_persists_and_combines_lp_and_hyp(self):
        monitor = server.NeutralisMonitor("performance-test")
        monitor.config = {**monitor.config, "source": "orca", "positionAddress": "position", "hyperliquidAccount": "0x1111111111111111111111111111111111111111"}
        position = {"positionAddress": "position", "liquidityUsd": Decimal("10000")}
        hyp = server.HypState("xyz:IBM", 2, Decimal("230"), Decimal("230"), Decimal("-10"), 0, entry_price=Decimal("232"))
        with patch.object(monitor, "_event"), patch.object(server, "hyp_performance_since", return_value={
            "realizedPnlUsd": Decimal("15"), "feesUsd": Decimal("2"), "fundingUsd": Decimal("1")
        }):
            first = monitor._performance_metrics(position, hyp, True)
            position["liquidityUsd"] = Decimal("9900")
            hyp.mark = Decimal("229")
            second = monitor._performance_metrics(position, hyp, True)
        self.assertTrue(monitor.performance_state_file.exists())
        self.assertEqual(first["combinedTrackedPnlUsd"], Decimal("14"))
        self.assertEqual(second["lpTrackedPnlUsd"], Decimal("-100"))
        self.assertEqual(second["hypOpenPnlUsd"], Decimal("10"))
        self.assertEqual(second["combinedTrackedPnlUsd"], Decimal("-76"))

    def test_performance_decomposes_lp_fees_rebalancing_and_benchmarks(self):
        monitor = server.NeutralisMonitor("performance-decomposition")
        monitor.config = {**monitor.config, "source": "byreal", "positionAddress": "position", "hyperliquidAccount": "0x1111111111111111111111111111111111111111"}
        position = {
            "positionAddress": "position", "liquidityUsd": Decimal("10000"),
            "currentPrice": Decimal("100"), "lowerPrice": Decimal("90"),
            "upperPrice": Decimal("110"), "earnedUsd": Decimal("50"),
        }
        hyp = server.HypState("xyz:IBM", 2, Decimal("100"), Decimal("100"), Decimal("0"), 0)
        history = {"realizedPnlUsd": Decimal("0"), "feesUsd": Decimal("0"), "fundingUsd": Decimal("0")}
        with patch.object(monitor, "_event"), patch.object(server, "hyp_performance_since", return_value=history):
            monitor._performance_metrics(position, hyp, True)
            position.update({"liquidityUsd": Decimal("9800"), "currentPrice": Decimal("90"), "earnedUsd": Decimal("60")})
            hyp.mark = Decimal("90")
            result = monitor._performance_metrics(position, hyp, True)

        self.assertEqual(result["lpPrincipalPnlUsd"], Decimal("-200"))
        self.assertEqual(result["lpFeesPnlUsd"], Decimal("10"))
        self.assertEqual(result["lpTrackedPnlUsd"], Decimal("-190"))
        self.assertIsNotNone(result["lpPassiveMixPnlUsd"])
        self.assertEqual(
            result["lpRebalancingEffectUsd"],
            result["lpPrincipalPnlUsd"] - result["lpPassiveMixPnlUsd"],
        )
        self.assertEqual(result["assetBuyHoldPnlUsd"], Decimal("-1000"))
        self.assertEqual(result["advantageVsLpUsd"], Decimal("0"))
        self.assertEqual(result["advantageVsBuyHoldUsd"], Decimal("810"))
        self.assertTrue(result["lpFeesAvailable"])

    def test_execution_slippage_is_attribution_and_persists(self):
        monitor = server.NeutralisMonitor("performance-slippage")
        monitor.performance_state = {"version": 1, "hypExecutionSlippageUsd": "0"}
        monitor.performance_state_file = Path(TEST_DATA.name) / "performance-slippage.json"

        monitor._record_execution_slippage(Decimal("100"), Decimal("101"), Decimal("10"), True)
        monitor._record_execution_slippage(Decimal("100"), Decimal("99"), Decimal("5"), False)

        self.assertEqual(Decimal(monitor.performance_state["hypExecutionSlippageUsd"]), Decimal("15"))
        self.assertTrue(monitor.performance_state_file.exists())

    def test_xstock_symbol_maps_to_hyperliquid(self):
        self.assertEqual(server.hyp_symbol("AAPLX"), "AAPL")
        self.assertEqual(server.hyp_symbol("AMZNx"), "AMZN")
        self.assertEqual(server.hyp_symbol("CRCLX"), "CRCL")
        self.assertEqual(server.hyp_symbol("COINX"), "COIN")
        self.assertEqual(server.hyp_symbol("SPCX"), "SPCX")
        self.assertEqual(server.hyp_symbol("SPCXx"), "SPCX")
        self.assertEqual(server.hyp_symbol("SpaceX"), "SPCX")
        self.assertEqual(server.hyp_symbol("SpaceXx"), "SPCX")
        self.assertEqual(server.hyp_symbol("NVDAx"), "NVDA")
        self.assertEqual(server.hyp_symbol("NVIDIA"), "NVDA")
        self.assertEqual(server.hyp_symbol("GOOGLx"), "GOOGL")
        self.assertEqual(server.hyp_symbol("Google"), "GOOGL")
        self.assertEqual(server.hyp_symbol("Alphabet"), "GOOGL")
        self.assertEqual(server.hyp_symbol("wNEAR"), "NEAR")
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
        wallet = "11111111111111111111111111111111"
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
        wallet = "11111111111111111111111111111111"
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
        wallet = "11111111111111111111111111111111"
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

    def test_three_monitors_have_independent_persistent_files(self):
        self.assertEqual(set(server.MONITORS), {"1", "2", "3"})
        third = server.monitor_for_slot("3")
        self.assertEqual(third.slot, "3")
        self.assertEqual(third.config_file.name, "config-3.json")
        self.assertEqual(third.log_file.name, "events-3.jsonl")
        self.assertEqual(third.strategy_state_file.name, "strategy-state-3.json")
        self.assertEqual(len({monitor.config_file for monitor in server.MONITORS.values()}), 3)
        self.assertEqual(len({monitor.strategy_state_file for monitor in server.MONITORS.values()}), 3)

    def test_byreal_position_normalization(self):
        position = {
            "positionAddress": "11111111111111111111111111111111",
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

    def test_network_failures_are_treated_as_transient(self):
        monitor = server.NeutralisMonitor("network-retry-test")
        self.assertTrue(monitor._is_transient_network_error(server.NeutralisError("Falha de rede ao consultar api.hyperliquid.xyz")))
        self.assertFalse(monitor._is_transient_network_error(server.NeutralisError("Contrato não encontrado na Hyperliquid")))

    def test_retry_snapshot_records_network_recovery(self):
        monitor = server.NeutralisMonitor("network-recovery-test")
        expected = ({}, Mock(), Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"))
        events = []
        with patch.object(
            monitor, "_live_snapshot", side_effect=[server.NeutralisError("Falha de rede ao consultar api.hyperliquid.xyz"), expected]
        ), patch.object(monitor.stop_event, "wait", return_value=False), patch.object(
            monitor, "_event", side_effect=lambda event, message, **details: events.append((event, message, details))
        ):
            self.assertEqual(monitor._retry_snapshot(), expected)
        self.assertEqual([item[0] for item in events], ["network-retry", "network-recovered"])
        self.assertIn("api.hyperliquid.xyz", events[0][1])

    def test_retry_snapshot_keeps_monitor_alive_when_position_temporarily_disappears(self):
        monitor = server.NeutralisMonitor("position-retry-test")
        expected = ({"positionAddress": "position"}, Mock(), Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"))
        events = []
        unavailable = server.NeutralisError("A posição selecionada não está aberta ou não pode ser calculada")
        with patch.object(monitor, "_live_snapshot", side_effect=[unavailable, expected]), patch.object(
            monitor.stop_event, "wait", return_value=False
        ), patch.object(monitor, "_event", side_effect=lambda event, message, **details: events.append(event)):
            self.assertEqual(monitor._retry_snapshot(), expected)

        self.assertEqual(events, ["position-retry", "position-recovered"])

    def test_live_snapshot_falls_back_to_hyp_mark_when_byreal_has_no_tick_price(self):
        monitor = server.NeutralisMonitor("byreal-mark-fallback-test")
        position = {
            "liquidityUsd": Decimal("1000"), "lowerPrice": Decimal("100"), "upperPrice": Decimal("200"),
            "currentPrice": None, "normalizedLiquidity": Decimal("10"), "hedgeSymbol": "AAPL", "assetSymbol": "AAPLX",
        }
        hyp = server.HypState("xyz:AAPL", 3, Decimal("150"), Decimal("150"), Decimal("0"), 0)
        with patch.object(monitor, "_selected_position", return_value=position), patch.object(server, "hyp_state", return_value=hyp):
            _, _, _, _, _, target = monitor._live_snapshot()
        self.assertGreater(target, 0)

    def test_byreal_mint_price_requires_exact_mint(self):
        mint = "11111111111111111111111111111111"
        payload = {"result": {"data": {"records": [
            {"mintAddress": "21111111111111111111111111111111", "priceUsd": "999"},
            {"mintAddress": mint, "priceUsd": "177.73"},
        ]}}}
        with patch.object(server, "json_request", return_value=payload):
            self.assertEqual(server.byreal_mint_price(mint), 177.73)

    def test_byreal_positions_enriches_price_from_mint_catalog(self):
        wallet = "11111111111111111111111111111111"
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
            positions = server.byreal_positions("11111111111111111111111111111111")
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

    def test_monitor_uses_configured_manual_rebalance_step(self):
        original = dict(server.MONITOR.config)
        try:
            server.MONITOR.config["stepPercent"] = "0.25"
            self.assertEqual(server.MONITOR.rebalance_step(Decimal("90"), Decimal("110")), Decimal("0.0025"))
        finally:
            server.MONITOR.config = original

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
        self.assertIsNone(server.hyp_dex("PENGU"))

    def test_ibm_uses_xyz_hyperliquid_market(self):
        self.assertEqual(server.hyp_symbol("IBM"), "IBM")
        self.assertEqual(server.hyp_dex("IBM"), "xyz")

    def test_uniswap_config_accepts_evm_wallet_and_v4_pool_id(self):
        monitor = server.NeutralisMonitor("uniswap-test")
        monitor.config_file = Path(TEST_DATA.name) / "uniswap-config.json"
        pool_id = "0x6fd1e411116a0d3df88e6fec47ade148941e6e43ab886e43eb7f59b166e1ef0f"
        result = monitor.save_config({
            "source": "uniswap",
            "evmWallet": "0x1111111111111111111111111111111111111111",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "positionAddress": pool_id,
            "uniswapTokenId": "42",
            "maxPositionNotional": "1000",
            "stepPercent": "0.5",
        })
        self.assertEqual(result["source"], "uniswap")
        self.assertEqual(result["positionAddress"], pool_id)
        self.assertEqual(result["uniswapTokenId"], "42")

    def test_uniswap_config_accepts_v3_pool_address_in_same_field(self):
        monitor = server.NeutralisMonitor("uniswap-v3-config-test")
        monitor.config_file = Path(TEST_DATA.name) / "uniswap-v3-config.json"
        pool = "0x34D0dC122CF9A8Eb296fC5e0D3A233625D7d19b7"
        result = monitor.save_config({
            "source": "uniswap",
            "evmWallet": "0x1111111111111111111111111111111111111111",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "positionAddress": pool,
            "uniswapTokenId": "729115",
            "maxPositionNotional": "1000",
            "stepPercent": "0.5",
        })
        self.assertEqual(result["positionAddress"], pool)
        self.assertEqual(result["uniswapTokenId"], "729115")

    def test_uniswap_manual_token_id_skips_blockscout_discovery(self):
        monitor = server.NeutralisMonitor("uniswap-manual-token-test")
        pool_id = "0x" + "a" * 64
        monitor.config.update({"source": "uniswap", "positionAddress": pool_id, "uniswapTokenId": "42"})
        position = {"positionAddress": "42"}
        with patch.object(server, "uniswap_v4_position", return_value=position) as direct, patch.object(server, "uniswap_v4_positions") as discovery:
            self.assertEqual(monitor.positions(), [position])
        direct.assert_called_once_with(42, pool_id)
        discovery.assert_not_called()

    def test_uniswap_v3_address_routes_to_v3_reader(self):
        monitor = server.NeutralisMonitor("uniswap-v3-route-test")
        pool = "0x34D0dC122CF9A8Eb296fC5e0D3A233625D7d19b7"
        monitor.config.update({"source": "uniswap", "positionAddress": pool, "uniswapTokenId": "729115"})
        position = {"source": "uniswap", "positionAddress": "729115", "poolAddress": pool}
        with patch.object(server, "uniswap_v3_position", return_value=position) as v3, patch.object(server, "uniswap_v4_position") as v4:
            self.assertEqual(monitor.positions(), [position])
        v3.assert_called_once_with(729115, pool)
        v4.assert_not_called()

    def test_uniswap_v3_position_decodes_position_and_pool(self):
        pool = "0x34D0dC122CF9A8Eb296fC5e0D3A233625D7d19b7"
        token0 = "0x" + "11" * 20
        token1 = "0x" + "22" * 20
        words = [0, 0, int(token0, 16), int(token1, 16), 500, (-100) & ((1 << 256) - 1), 100, 123456, 0, 0, 0, 0]
        position_data = "".join(f"{word:064x}" for word in words)
        address_word = lambda address: int(address, 16).to_bytes(32, "big").hex()
        calls = [position_data, address_word(token0), address_word(token1), f"{500:064x}", f"{2**96:064x}"]
        normalized = {"source": "uniswap", "poolAddress": pool}
        with patch.object(server, "robinhood_call", side_effect=calls), patch.object(
            server, "erc20_metadata", side_effect=[("GOOGL", 18), ("USDG", 6)]
        ), patch.object(server, "concentrated_position_result", return_value=normalized) as convert:
            self.assertEqual(server.uniswap_v3_position(729115, pool), normalized)
        args = convert.call_args.args
        self.assertEqual(args[:7], ("uniswap", 729115, pool, "GOOGL", 18, "USDG", 6))
        self.assertEqual(args[8:], (-100, 100, 123456))

    def test_uniswap_manual_token_id_reports_unreadable_position(self):
        monitor = server.NeutralisMonitor("uniswap-manual-token-error-test")
        monitor.config.update({"source": "uniswap", "positionAddress": "0x" + "a" * 64, "uniswapTokenId": "42"})
        with patch.object(server, "uniswap_v4_position", return_value=None):
            with self.assertRaisesRegex(server.NeutralisError, "NFT Uniswap não corresponde"):
                monitor.positions()

    def test_uniswap_helpers_decode_signed_tick(self):
        self.assertEqual(server.abi_int24(0x7FFFFF), 8388607)
        self.assertEqual(server.abi_int24(0xFFFFFF), -1)

    def test_hyp_ioc_price_respects_asset_tick_and_direction(self):
        # Com szDecimals=2, o preço pode ter no máximo quatro casas decimais.
        self.assertEqual(server.hyp_ioc_limit_price(Decimal("0.028742"), Decimal("0.005"), True, 2), Decimal("0.0289"))
        self.assertEqual(server.hyp_ioc_limit_price(Decimal("0.028742"), Decimal("0.005"), False, 2), Decimal("0.0285"))

    def test_uniswap_uses_second_rpc_when_official_rpc_fails(self):
        with patch.object(server, "json_request", side_effect=[server.NeutralisError("offline"), {"result": "0x123"}]) as request:
            self.assertEqual(server.robinhood_request("eth_chainId", []), "0x123")
        self.assertEqual(request.call_count, 2)

    def test_uniswap_nft_discovery_uses_blockscout_not_alchemy_logs(self):
        wallet = "0x1111111111111111111111111111111111111111"
        payload = {"items": [{"token": {"address": server.UNISWAP_V4_POSITION_MANAGER}, "token_instances": [{"id": "42"}, {"token_id": "43"}]}]}
        with patch.object(server, "json_request", return_value=payload) as request:
            self.assertEqual(server.uniswap_v4_owner_tokens(wallet), [42, 43])
        self.assertIn("/nft/collections", request.call_args.args[0])

    def test_orca_skr_usdc_uses_main_hyperliquid_skr_market(self):
        skr = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3"
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertEqual(server.orca_symbols([skr, usdc]), {skr: "SKR", usdc: "USDC"})
        self.assertEqual(server.hyp_symbol("SKR"), "SKR")
        self.assertIsNone(server.hyp_dex("SKR"))

    def test_orca_avax_usdc_uses_main_hyperliquid_avax_market(self):
        avax = "avaxGHCq3T7hoxd73oY2KY9hJSTaeMibXvHy5KNzh5D"
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertEqual(server.orca_symbols([avax, usdc]), {avax: "AVAX", usdc: "USDC"})
        self.assertEqual(server.hyp_symbol("AVAX"), "AVAX")
        self.assertIsNone(server.hyp_dex("AVAX"))

    def test_byreal_wnear_usdc_uses_main_hyperliquid_near_market(self):
        pool = {
            "poolAddress": "FXetFeCdbzdoQQdyxhDcjH29VUjyA2pj2FZZhV7xgw8f",
            "mintAInfo": {"address": "3ZLek6pGAQ2B21K6VJvUXQGqLq9xw9BG", "symbol": "wNEAR", "decimals": 6},
            "mintBInfo": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "symbol": "USDC", "decimals": 6},
            "tickCurrent": 8500,
        }
        position = {
            "positionAddress": "11111111111111111111111111111111",
            "poolAddress": pool["poolAddress"],
            "lowerTick": 8000,
            "upperTick": 9000,
            "liquidityUsd": 1000,
        }
        normalized = server.normalize_position(position, pool)
        self.assertEqual(normalized["pair"], "WNEAR / USDC")
        self.assertEqual(normalized["hedgeSymbol"], "NEAR")
        self.assertIsNone(server.hyp_dex(normalized["hedgeSymbol"]))
        self.assertTrue(normalized["importable"])

    def test_main_hyperliquid_market_is_queried_without_dex(self):
        account = "0x1111111111111111111111111111111111111111"
        metadata = {"universe": [{"name": "ZEC", "szDecimals": 3}]}
        contexts = [{"markPx": "42", "oraclePx": "42"}]
        clearinghouse = {"assetPositions": [{"position": {"coin": "ZEC", "szi": "-1.2", "entryPx": "41.75"}}]}
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), clearinghouse, []]) as request:
            hyp = server.hyp_state(account, "ZEC")
        self.assertEqual(hyp.market, "ZEC")
        self.assertEqual(hyp.signed_position, Decimal("-1.2"))
        self.assertEqual(hyp.entry_price, Decimal("41.75"))
        self.assertTrue(all("dex" not in call.args[1] for call in request.call_args_list))

    def test_ibm_uses_active_hyperliquid_catalog_name(self):
        account = "0x1111111111111111111111111111111111111111"
        metadata = {"universe": [{"name": "IBM", "szDecimals": 2}]}
        contexts = [{"markPx": "240", "oraclePx": "240"}]
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), {"assetPositions": []}, []]):
            hyp = server.hyp_state(account, "IBM")
        self.assertEqual(hyp.market, "xyz:IBM")
        self.assertEqual(server.HYP_MARKET_ALTERNATIVES["IBM"], ("IBM",))

    def test_aapl_uses_usd_suffix_when_that_is_the_active_catalog_name(self):
        account = "0x1111111111111111111111111111111111111111"
        metadata = {"universe": [{"name": "AAPLUSD", "szDecimals": 2}]}
        contexts = [{"markPx": "240", "oraclePx": "240"}]
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), {"assetPositions": []}, []]):
            hyp = server.hyp_state(account, "AAPL")
        self.assertEqual(hyp.market, "xyz:AAPLUSD")

    def test_hyp_finds_stock_after_it_moves_to_another_perp_dex(self):
        account = "0x1111111111111111111111111111111111111111"
        empty_meta, empty_contexts = {"universe": []}, []
        active_meta, active_contexts = {"universe": [{"name": "AAPL", "szDecimals": 2}]}, [{"markPx": "240", "oraclePx": "240"}]
        with patch.object(server, "json_request", side_effect=[(empty_meta, empty_contexts), [{"name": "other"}], (active_meta, active_contexts), {"assetPositions": []}, []]):
            hyp = server.hyp_state(account, "AAPL")
        self.assertEqual(hyp.market, "other:AAPL")

    def test_hyp_accepts_fully_qualified_hip3_catalog_name(self):
        account = "0x1111111111111111111111111111111111111111"
        metadata = {"universe": [{"name": "xyz:AAPL", "szDecimals": 2}]}
        contexts = [{"markPx": "240", "oraclePx": "240"}]
        clearinghouse = {"assetPositions": [{"position": {"coin": "xyz:AAPL", "szi": "-2"}}]}
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), clearinghouse, []]):
            hyp = server.hyp_state(account, "AAPL")
        self.assertEqual(hyp.market, "xyz:AAPL")
        self.assertEqual(hyp.signed_position, Decimal("-2"))

    def test_hyp_finds_spacex_nvidia_and_google_contracts(self):
        account = "0x1111111111111111111111111111111111111111"
        metadata = {"universe": [
            {"name": "xyz:NVDA", "szDecimals": 3},
            {"name": "xyz:SPCX", "szDecimals": 2},
            {"name": "xyz:GOOGL", "szDecimals": 3},
        ]}
        contexts = [
            {"markPx": "227.73", "oraclePx": "227.73"},
            {"markPx": "149.29", "oraclePx": "149.29"},
            {"markPx": "342.86", "oraclePx": "342.86"},
        ]
        for source_symbol, expected_market in (
            ("NVDAx", "xyz:NVDA"), ("SpaceXx", "xyz:SPCX"), ("GOOGLx", "xyz:GOOGL")
        ):
            with self.subTest(source_symbol=source_symbol), patch.object(
                server, "json_request", side_effect=[(metadata, contexts), {"assetPositions": []}, []]
            ):
                hyp = server.hyp_state(account, source_symbol)
                self.assertEqual(hyp.market, expected_market)

    def test_byreal_amazon_maps_to_active_xyz_contract(self):
        position = {"lowerTick": -100, "upperTick": 100, "liquidityUsd": 1000}
        pool = {
            "poolAddress": "pool", "tickCurrent": 0,
            "mintA": {"symbol": "AMZNx", "decimals": 6},
            "mintB": {"symbol": "USDC", "decimals": 6},
        }
        normalized = server.normalize_position(position, pool)
        self.assertEqual(normalized["assetSymbol"], "AMZNX")
        self.assertEqual(normalized["hedgeSymbol"], "AMZN")
        metadata = {"universe": [{"name": "xyz:AMZN", "szDecimals": 3}]}
        contexts = [{"markPx": "245.50", "oraclePx": "245.45"}]
        with patch.object(server, "json_request", side_effect=[(metadata, contexts), {"assetPositions": []}, []]):
            hyp = server.hyp_state("0x1111111111111111111111111111111111111111", "AMZNx")
        self.assertEqual(hyp.market, "xyz:AMZN")
        self.assertEqual(hyp.decimals, 3)

    def test_robinhood_accepts_equity_aliases(self):
        for symbol in (
            "NVDA", "NVDAx", "NVIDIA", "SPCX", "SPCXx", "SpaceX", "SpaceXx",
            "GOOGL", "GOOGLx", "Google", "Alphabet",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(server.hyp_symbol(symbol), server.ROBINHOOD_UNISWAP_ASSETS)

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

    def test_live_monitor_can_restart_after_manual_stop(self):
        monitor = server.NeutralisMonitor("restart-after-stop-test")
        monitor.stop_event.set()
        monitor.manual_stop_requested = True
        position = {"assetSymbol": "COINX", "hedgeSymbol": "COIN"}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("0"), 0)
        api_key_file = Mock()
        api_key_file.exists.return_value = True
        snapshot = (position, hyp, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"))
        with patch.object(monitor, "_live_snapshot", return_value=snapshot) as live_snapshot, patch.object(
            server, "API_KEY_FILE", api_key_file
        ), patch.object(server.threading, "Thread"):
            monitor.start(live=True, confirmation="ATIVAR")
        live_snapshot.assert_called_once()
        self.assertFalse(monitor.stop_event.is_set())
        self.assertFalse(monitor.manual_stop_requested)
        self.assertEqual(monitor.state["mode"], "starting")

    def test_api_wallet_key_is_never_returned(self):
        key = "11" * 32
        result = server.MONITOR.save_api_key({"privateKey": key})
        self.assertEqual(result, {"configured": True})
        self.assertNotIn(key, str(server.MONITOR.public_state()))

    def test_api_wallet_rejects_invalid_key(self):
        with self.assertRaisesRegex(server.NeutralisError, "inválida"):
            server.MONITOR.save_api_key({"privateKey": "segredo"})

    def test_telegram_alert_is_secret_and_only_sent_for_automatic_pause(self):
        token = "123456:abcdefghijklmnopqrstuvwxyzABCDE"
        server.MONITOR.save_telegram_alert({"botToken": token, "chatId": "123456789"})
        self.assertTrue(server.MONITOR.public_state()["telegramConfigured"])
        self.assertNotIn(token, str(server.MONITOR.public_state()))
        server.MONITOR.manual_stop_requested = False
        with patch.object(server, "json_request", return_value={"ok": True}) as request:
            server.MONITOR._pause("falha de teste")
        self.assertIn("api.telegram.org", request.call_args.args[0])
        server.MONITOR.manual_stop_requested = True
        with patch.object(server, "json_request") as request:
            server.MONITOR._pause("não deve alertar")
        request.assert_not_called()
        server.MONITOR.manual_stop_requested = False

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

    def test_unfilled_ioc_keeps_retrying_until_manual_stop(self):
        position = {"positionAddress": "position", "hedgeSymbol": "COIN", "currentPrice": Decimal("176")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        snapshot = (position, hyp, Decimal("150"), Decimal("200"), Decimal("1"), Decimal("2.600"))
        response = {"status": "ok", "response": {"data": {"statuses": [{"error": "IocCancel"}]}}}
        exchange = Mock()
        exchange.order.return_value = response
        with patch.object(server, "hyp_state", return_value=hyp), patch.object(server.MONITOR, "_live_snapshot", return_value=snapshot), patch.object(server.MONITOR, "_exchange", return_value=exchange), patch.object(server.MONITOR, "_event"), patch.object(server.MONITOR.stop_event, "wait", side_effect=[False, False, False, True]):
            with self.assertRaisesRegex(server.NeutralisError, "Monitor interrompido durante o ajuste"):
                server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("2.600"))
        self.assertEqual(exchange.order.call_count, 4)

    def test_insufficient_margin_reduces_order_and_keeps_monitor_running(self):
        position = {"positionAddress": "position", "hedgeSymbol": "COIN", "currentPrice": Decimal("100")}
        hyp = server.HypState("xyz:COIN", 3, Decimal("100"), Decimal("100"), Decimal("-2"), 0)
        snapshot = (position, hyp, Decimal("80"), Decimal("120"), Decimal("1"), Decimal("3"))
        response = {
            "status": "ok",
            "response": {"data": {"statuses": [{"error": "Insufficient margin to place order. asset=74"}]}},
        }
        exchange = Mock()
        exchange.order.return_value = response
        events = []
        with patch.object(server, "hyp_state", return_value=hyp), patch.object(
            server.MONITOR, "_retry_snapshot", return_value=snapshot
        ), patch.object(server.MONITOR, "_exchange", return_value=exchange), patch.object(
            server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)
        ), patch.object(server.MONITOR.stop_event, "wait", return_value=False):
            result = server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("3"))

        self.assertIsNone(result)
        self.assertGreater(exchange.order.call_count, 1)
        self.assertIn("margin-retry", events)
        self.assertIn("margin-limited", events)

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

    def test_live_monitor_preserves_existing_short_without_trusted_state(self):
        position = {
            "positionAddress": "position",
            "assetSymbol": "COINX",
            "hedgeSymbol": "COIN",
            "currentPrice": Decimal("176"),
        }
        initial = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        before = (position, initial, Decimal("159"), Decimal("195"), Decimal("755"), Decimal("2.600"))
        events = []
        with patch.object(server.MONITOR, "_live_snapshot", return_value=before), patch.object(server.MONITOR, "_execute_auto_adjustment") as adjustment, patch.object(server.MONITOR.stop_event, "wait", return_value=True), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
            server.MONITOR._run(live=True)
        adjustment.assert_not_called()
        self.assertEqual(events, ["start-live"])
        self.assertEqual(server.MONITOR.state["snapshot"]["anchor"], 176.0)
        self.assertEqual(server.MONITOR.state["snapshot"]["realShort"], 2.74)

    def test_arc_crcl_usdc_position_uses_crcl_units_for_hedge(self):
        result = server.concentrated_position_result(
            "uniswap", 57757, "0x" + "12" * 32,
            "CRCL", 18, "USDC", 6,
            707448682476069171114990, -232696, -231251,
            4069783452985025,
            quote_symbols={"USDC"}, allowed_assets={"CRCL"},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["pair"], "CRCL / USDC")
        self.assertEqual(result["hedgeSymbol"], "CRCL")
        self.assertEqual(result["quoteSymbol"], "USDC")
        self.assertGreater(result["assetAmount"], Decimal("0"))
        self.assertTrue(result["importable"])

    def test_arc_source_requires_numeric_nft_and_accepts_empty_pool_id(self):
        monitor = server.NeutralisMonitor("arc-test")
        monitor.config_file = Path(TEST_DATA.name) / "arc-test-config.json"
        saved = monitor.save_config({
            "source": "uniswap_arc",
            "evmWallet": "0x1111111111111111111111111111111111111111",
            "uniswapTokenId": "57757",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "positionAddress": "",
            "maxPositionNotional": "5000",
            "stepPercent": "0.5",
        })
        self.assertEqual(saved["source"], "uniswap_arc")
        self.assertEqual(saved["uniswapTokenId"], "57757")

    def test_upside_strategy_returns_to_wait_at_reference_and_uses_half_step(self):
        step = Decimal("0.005")
        # O preço médio do short não encerra mais a proteção-base.
        self.assertIsNone(server.upside_hedge_signal("protected", Decimal("79.92"), Decimal("80"), step, Decimal("79"), True))
        self.assertEqual(server.upside_hedge_signal("protected", Decimal("80"), Decimal("80"), step, Decimal("79")), "close")
        self.assertEqual(server.upside_hedge_signal("protected", Decimal("80.01"), Decimal("80"), step, Decimal("81")), "close")
        self.assertIsNone(server.upside_hedge_signal("upside", Decimal("80.01"), Decimal("80"), step))
        self.assertEqual(server.upside_hedge_signal("upside", Decimal("80"), Decimal("80"), step), "wait")
        self.assertIsNone(server.upside_hedge_signal("initial_wait", Decimal("79.81"), Decimal("80"), step))
        self.assertEqual(server.upside_hedge_signal("initial_wait", Decimal("79.80"), Decimal("80"), step), "open")
        self.assertEqual(server.upside_hedge_signal("initial_wait", Decimal("80.20"), Decimal("80"), step), "confirm_upside")
        self.assertEqual(server.upside_hedge_signal("direction_wait", Decimal("79.80"), Decimal("80"), step), "open")

    def test_principal_result_excludes_fees_and_rewards(self):
        position = {"liquidityUsd": Decimal("12548.56"), "earnedUsd": Decimal("403.58")}
        metrics = server.principal_metrics(position, "12627.51")

        self.assertEqual(metrics["principalPnlUsd"], Decimal("-78.95"))
        self.assertAlmostEqual(float(metrics["principalPnlPercent"]), -0.6252, places=4)

    def test_principal_result_requires_explicit_initial_balance(self):
        metrics = server.principal_metrics({"liquidityUsd": Decimal("12548.56")}, "")
        self.assertIsNone(metrics["principalInitialUsd"])
        self.assertIsNone(metrics["principalPnlUsd"])

    def test_strategy_state_round_trip_preserves_upside_reference(self):
        monitor = server.NeutralisMonitor("persistence")
        monitor.strategy_state_file.unlink(missing_ok=True)
        monitor.persisted_strategy = None
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "positionAddress": "actual-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "actual-position"}
        snapshot = {
            "position": position,
            "market": "ZEC",
            "hedgeStrategy": "upside",
            "hedgeRegime": "upside",
            "protectionReference": Decimal("42.75"),
            "realShort": Decimal("0"),
        }
        monitor._persist_strategy_state(snapshot)

        restarted = server.NeutralisMonitor("persistence")
        restarted.config = dict(monitor.config)
        hyp = server.HypState("ZEC", 3, Decimal("44"), Decimal("44"), Decimal("0"), 0)
        restored = restarted._restore_strategy_state(position, hyp, "upside")

        self.assertEqual(restored, ("upside", Decimal("42.75")))

    def test_protected_state_restores_fixed_reference_not_current_short_average(self):
        monitor = server.NeutralisMonitor("fixed-reference")
        monitor.strategy_state_file.unlink(missing_ok=True)
        monitor.persisted_strategy = None
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "positionAddress": "actual-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "actual-position"}
        monitor._persist_strategy_state({
            "position": position,
            "market": "NEAR",
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": Decimal("4.12"),
            "realShort": Decimal("10"),
        })

        restarted = server.NeutralisMonitor("fixed-reference")
        restarted.config = dict(monitor.config)
        hyp = server.HypState("NEAR", 3, Decimal("4.08"), Decimal("4.08"), Decimal("-10"), 0, entry_price=Decimal("4.10"))

        self.assertEqual(restarted._restore_strategy_state(position, hyp, "upside"), ("protected", Decimal("4.12")))

    def test_legacy_near_reference_is_corrected_to_420_once(self):
        monitor = server.NeutralisMonitor("near-reference-correction")
        monitor.config = {
            **monitor.config,
            "source": "byreal",
            "positionAddress": "near-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "near-position"}
        monitor.persisted_strategy = {
            "version": 4,
            "source": "byreal",
            "positionAddress": "near-position",
            "market": "NEAR",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": "4.10",
        }
        hyp = server.HypState(
            "NEAR", 3, Decimal("4.08"), Decimal("4.08"), Decimal("-10"), 0,
            entry_price=Decimal("4.10"),
        )

        self.assertEqual(
            monitor._restore_strategy_state(position, hyp, "upside"),
            ("protected", Decimal("4.20")),
        )

    def test_current_near_reference_at_410_is_not_corrected_again(self):
        monitor = server.NeutralisMonitor("near-reference-current")
        monitor.config = {
            **monitor.config,
            "source": "byreal",
            "positionAddress": "near-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "near-position"}
        monitor.persisted_strategy = {
            "version": 5,
            "source": "byreal",
            "positionAddress": "near-position",
            "market": "NEAR",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": "4.10",
        }
        hyp = server.HypState("NEAR", 3, Decimal("4.08"), Decimal("4.08"), Decimal("-10"), 0)

        self.assertEqual(
            monitor._restore_strategy_state(position, hyp, "upside"),
            ("protected", Decimal("4.10")),
        )

    def test_version_5_avax_reference_is_corrected_once_and_recovery_is_armed(self):
        monitor = server.NeutralisMonitor("avax-reference-current-operation")
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "positionAddress": "avax-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "avax-position"}
        monitor.persisted_strategy = {
            "version": 5,
            "source": "orca",
            "positionAddress": "avax-position",
            "market": "AVAX",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": "11.24",
        }
        hyp = server.HypState("AVAX", 2, Decimal("11.25"), Decimal("11.25"), Decimal("-10"), 0, entry_price=Decimal("10.99"))

        self.assertEqual(
            monitor._restore_strategy_state(position, hyp, "upside"),
            ("protected", Decimal("11.08")),
        )
        self.assertTrue(monitor.persisted_strategy["baseRecoveryArmed"])
        self.assertTrue(monitor.persisted_strategy["baseRecoveryForceClose"])

    def test_version_5_near_reference_is_corrected_once_and_recovery_is_armed(self):
        monitor = server.NeutralisMonitor("near-reference-current-operation")
        monitor.config = {
            **monitor.config,
            "source": "byreal",
            "positionAddress": "near-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "near-position"}
        monitor.persisted_strategy = {
            "version": 5,
            "source": "byreal",
            "positionAddress": "near-position",
            "market": "NEAR",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": "4.28",
        }
        hyp = server.HypState("NEAR", 3, Decimal("4.29"), Decimal("4.29"), Decimal("-10"), 0, entry_price=Decimal("4.0772"))

        self.assertEqual(
            monitor._restore_strategy_state(position, hyp, "upside"),
            ("protected", Decimal("4.10")),
        )
        self.assertTrue(monitor.persisted_strategy["baseRecoveryArmed"])
        self.assertTrue(monitor.persisted_strategy["baseRecoveryForceClose"])

    def test_version_6_reference_is_never_changed_by_one_time_correction(self):
        monitor = server.NeutralisMonitor("reference-correction-finished")
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "positionAddress": "avax-position",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "avax-position"}
        monitor.persisted_strategy = {
            "version": 6,
            "source": "orca",
            "positionAddress": "avax-position",
            "market": "AVAX",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": "11.24",
        }
        hyp = server.HypState("AVAX", 2, Decimal("11.20"), Decimal("11.20"), Decimal("-10"), 0)

        self.assertEqual(
            monitor._restore_strategy_state(position, hyp, "upside"),
            ("protected", Decimal("11.24")),
        )

    def test_manual_stop_preserves_fixed_operation_reference(self):
        monitor = server.NeutralisMonitor("manual-reset")
        monitor._persist_strategy_state({
            "position": {"positionAddress": "position"},
            "market": "NEAR",
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": Decimal("4.12"),
            "realShort": Decimal("10"),
        })

        monitor.stop()

        self.assertEqual(monitor.persisted_strategy["protectionReference"], "4.12")
        self.assertTrue(monitor.strategy_state_file.exists())

        restarted = server.NeutralisMonitor("manual-reset")
        self.assertEqual(restarted.persisted_strategy["protectionReference"], "4.12")

    def test_strategy_state_persists_lots_and_global_recovery_reference(self):
        monitor = server.NeutralisMonitor("lot-persistence")
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        lot = {"id": "lot-1", "size": "2.5", "entryPrice": "99", "openedAt": "now", "closedAt": None}
        monitor._persist_strategy_state({
            "position": {"positionAddress": "position"},
            "market": "NEAR",
            "hedgeStrategy": "upside",
            "hedgeRegime": "protected",
            "protectionReference": Decimal("100"),
            "realShort": Decimal("12.5"),
            "baseShort": Decimal("10"),
            "hedgeLots": [lot],
            "recoveryActive": True,
            "recoveryHigh": Decimal("101.25"),
            "recoveryReleasedSize": Decimal("2.5"),
        })

        self.assertEqual(monitor.persisted_strategy["version"], 8)
        self.assertEqual(monitor.persisted_strategy["hedgeLots"], [lot])
        self.assertEqual(monitor.persisted_strategy["recoveryHigh"], "101.25")
        self.assertEqual(monitor.persisted_strategy["recoveryReleasedSize"], "2.5")

    def test_contaminated_avax_reference_is_replaced_by_current_operation(self):
        monitor = server.NeutralisMonitor("3")
        monitor.config = {**monitor.config, "source": "orca", "positionAddress": "new-avax", "hyperliquidAccount": "0xabc", "hedgeStrategy": "upside"}
        position = {"positionAddress": "new-avax"}
        monitor.persisted_strategy = {
            "version": 7, "source": "orca", "positionAddress": "new-avax", "market": "AVAX",
            "hyperliquidAccount": "0xabc", "hedgeStrategy": "upside", "hedgeRegime": "upside",
            "protectionReference": "11.08",
        }
        hyp = server.HypState("AVAX", 2, Decimal("11.55"), Decimal("11.55"), Decimal("0"), 0)

        self.assertEqual(monitor._restore_strategy_state(position, hyp, "upside"), ("initial_wait", Decimal("11.55")))

    def test_existing_short_without_trusted_state_is_not_increased_at_start(self):
        monitor = server.NeutralisMonitor("safe-existing-short")
        monitor.persisted_strategy = None
        monitor.config = {**monitor.config, "hedgeStrategy": "upside", "stepPercent": "1.25", "maxPositionNotional": "10000"}
        position = {
            "positionAddress": "position", "hedgeSymbol": "AVAX", "currentPrice": Decimal("11.10"),
            "hedgeMode": "units",
        }
        initial = server.HypState("AVAX", 2, Decimal("11.10"), Decimal("11.10"), Decimal("-348.37"), 0, entry_price=Decimal("10.99"))
        snapshot = (position, initial, Decimal("10.23"), Decimal("11.93"), Decimal("4500"), Decimal("402.88"))

        with patch.object(monitor, "_retry_snapshot", return_value=snapshot), patch.object(
            monitor.stop_event, "wait", return_value=True
        ), patch.object(monitor, "_execute_auto_adjustment") as execute:
            monitor._run(live=True)

        execute.assert_not_called()
        self.assertAlmostEqual(monitor.state["snapshot"]["targetShort"], 348.37, places=6)

    def test_contaminated_near_reference_is_replaced_by_current_operation(self):
        monitor = server.NeutralisMonitor("1")
        monitor.config = {**monitor.config, "source": "orca", "positionAddress": "new-near", "hyperliquidAccount": "0xabc", "hedgeStrategy": "upside"}
        position = {"positionAddress": "new-near"}
        monitor.persisted_strategy = {
            "version": 7, "source": "orca", "positionAddress": "new-near", "market": "NEAR",
            "hyperliquidAccount": "0xabc", "hedgeStrategy": "upside", "hedgeRegime": "upside",
            "protectionReference": "4.10",
        }
        hyp = server.HypState("NEAR", 3, Decimal("4.55"), Decimal("4.55"), Decimal("0"), 0)

        self.assertEqual(monitor._restore_strategy_state(position, hyp, "upside"), ("initial_wait", Decimal("4.55")))

    def test_lot_is_consumed_without_affecting_other_lots(self):
        lots = []
        first = server.add_hedge_lot(lots, Decimal("3"), Decimal("99"))
        second = server.add_hedge_lot(lots, Decimal("2"), Decimal("98"))

        server.consume_hedge_lot(second, Decimal("2"))

        self.assertEqual(len(server.open_hedge_lots(lots)), 1)
        self.assertIs(server.open_hedge_lots(lots)[0], first)
        self.assertIsNotNone(second["closedAt"])

    def test_new_lot_is_ready_to_detect_immediate_reversal(self):
        lots = []
        lot = server.add_hedge_lot(lots, Decimal("2"), Decimal("3.9754"))

        self.assertTrue(lot["recoveryArmed"])
        self.assertFalse(lot["recoveryForceClose"])

    def test_lot_minimum_hold_blocks_early_close_and_expires_at_configured_time(self):
        lot = {
            "openedAt": "2026-09-22T12:00:00+00:00",
            "size": "10",
            "entryPrice": "10",
        }
        before = server.datetime.fromisoformat("2026-09-22T12:00:59+00:00")
        at_limit = server.datetime.fromisoformat("2026-09-22T12:01:00+00:00")

        self.assertFalse(server.lot_minimum_hold_elapsed(lot, Decimal("60"), before))
        self.assertTrue(server.lot_minimum_hold_elapsed(lot, Decimal("60"), at_limit))

    def test_lot_minimum_hold_is_saved_per_pool(self):
        monitor = server.NeutralisMonitor("hold-config")
        monitor.config_file = Path(TEST_DATA.name) / "hold-config.json"
        result = monitor.save_config({
            **monitor.config,
            "source": "orca",
            "solanaWallet": "11111111111111111111111111111111",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "lotMinHoldSeconds": "90",
        })

        self.assertEqual(result["lotMinHoldSeconds"], "90")
        self.assertEqual(monitor.lot_min_hold_seconds(), Decimal("90"))

    def test_lot_exit_mode_is_saved_per_pool(self):
        monitor = server.NeutralisMonitor("exit-mode-config")
        monitor.config_file = Path(TEST_DATA.name) / "exit-mode-config.json"
        result = monitor.save_config({
            **monitor.config,
            "source": "orca",
            "solanaWallet": "11111111111111111111111111111111",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "lotExitMode": "timer_emergency",
        })

        self.assertEqual(result["lotExitMode"], "timer_emergency")
        self.assertEqual(monitor.lot_exit_mode(), "timer_emergency")

    def test_invalid_lot_exit_mode_is_rejected(self):
        monitor = server.NeutralisMonitor("invalid-exit-mode")
        monitor.config_file = Path(TEST_DATA.name) / "invalid-exit-mode.json"

        with self.assertRaisesRegex(server.NeutralisError, "Método de saída"):
            monitor.save_config({
                **monitor.config,
                "solanaWallet": "11111111111111111111111111111111",
                "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
                "lotExitMode": "invalido",
            })

    def test_reentry_uses_one_global_half_trigger_and_does_not_accumulate(self):
        step = Decimal("0.01")
        high = Decimal("102")
        self.assertFalse(server.recovery_reentry_signal("protected", Decimal("101.50"), high, step))
        self.assertTrue(server.recovery_reentry_signal("protected", Decimal("101.49"), high, step))
        # Depois da recomposição o estado é zerado; quedas adicionais não
        # somam novas meias bandas sem uma nova parcela recuperada.
        self.assertFalse(server.recovery_reentry_signal("protected", Decimal("100"), Decimal("0"), step))

    def test_upside_regime_never_reopens_full_hedge_from_local_recovery_high(self):
        # Reproduz o defeito do histórico: uma queda curta desde a máxima local
        # ainda acima da referência inicial não pode abrir novamente 100% do hedge.
        self.assertFalse(
            server.recovery_reentry_signal(
                "upside", Decimal("11.16"), Decimal("11.24"), Decimal("0.0125")
            )
        )

    def test_base_short_is_not_treated_as_recoverable_lot(self):
        step = Decimal("0.0125")
        # Entrada média em 11,16 e pequena recuperação para 11,15. Como o
        # ativo ainda está abaixo da referência inicial, o short-base continua.
        self.assertIsNone(
            server.upside_hedge_signal(
                "protected", Decimal("11.15"), Decimal("11.20"), step,
                Decimal("11.16"), True,
            )
        )

    def test_lot_recovery_crosses_exit_floor_only_on_the_way_up(self):
        entry = Decimal("3.9754")
        floor = entry * (Decimal("1") - server.BASE_RECOVERY_EXIT_BUFFER)

        self.assertTrue(server.lot_recovery_crossed(floor - Decimal("0.0001"), floor, entry))
        self.assertTrue(server.lot_recovery_crossed(Decimal("3.96"), Decimal("3.99"), entry))
        self.assertFalse(server.lot_recovery_crossed(Decimal("4.00"), Decimal("3.97"), entry))
        self.assertFalse(server.lot_recovery_crossed(Decimal("3.96"), Decimal("3.97"), entry))

    def test_lot_minimum_time_does_not_close_by_itself(self):
        entry = Decimal("100")
        self.assertIsNone(
            server.lot_close_reason(Decimal("100.10"), Decimal("100.20"), entry, True)
        )

    def test_lot_normal_recovery_requires_hold_and_upward_crossing(self):
        entry = Decimal("100")
        floor = entry * (Decimal("1") - server.BASE_RECOVERY_EXIT_BUFFER)

        self.assertIsNone(
            server.lot_close_reason(floor - Decimal("0.01"), floor, entry, False)
        )
        self.assertEqual(
            server.lot_close_reason(floor - Decimal("0.01"), floor, entry, True),
            "recovery",
        )

    def test_lot_emergency_exit_ignores_minimum_time_at_four_tenths_percent(self):
        entry = Decimal("100")
        emergency = entry * (Decimal("1") + server.LOT_EMERGENCY_EXIT_RISE)

        self.assertIsNone(
            server.lot_close_reason(Decimal("100.30"), Decimal("100.399"), entry, False)
        )
        self.assertEqual(
            server.lot_close_reason(Decimal("100.30"), emergency, entry, False),
            "emergency",
        )

    def test_lot_emergency_follows_half_trigger_with_safety_limits(self):
        self.assertEqual(server.lot_emergency_exit_rise(Decimal("0.0175")), Decimal("0.00875"))
        self.assertEqual(server.lot_emergency_exit_rise(Decimal("0.002")), Decimal("0.0025"))
        self.assertEqual(server.lot_emergency_exit_rise(Decimal("0.05")), Decimal("0.015"))

        entry = Decimal("100")
        dynamic_rise = server.lot_emergency_exit_rise(Decimal("0.0175"))
        self.assertIsNone(
            server.lot_close_reason(
                Decimal("100.70"), Decimal("100.87"), entry, False, "cross_emergency", dynamic_rise
            )
        )
        self.assertEqual(
            server.lot_close_reason(
                Decimal("100.70"), Decimal("100.875"), entry, False, "cross_emergency", dynamic_rise
            ),
            "emergency",
        )

    def test_timer_mode_closes_at_expiration_without_emergency(self):
        entry = Decimal("100")
        self.assertIsNone(
            server.lot_close_reason(Decimal("100"), Decimal("100.50"), entry, False, "timer")
        )
        self.assertEqual(
            server.lot_close_reason(Decimal("100"), Decimal("100.50"), entry, True, "timer"),
            "timer",
        )

    def test_timer_emergency_mode_combines_both_exits(self):
        entry = Decimal("100")
        self.assertEqual(
            server.lot_close_reason(Decimal("100"), Decimal("100.40"), entry, False, "timer_emergency"),
            "emergency",
        )
        self.assertEqual(
            server.lot_close_reason(Decimal("99.80"), Decimal("99.95"), entry, True, "timer_emergency"),
            "timer",
        )

    def test_incremental_lot_closes_on_first_upward_crossing(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "NEAR", "currentPrice": Decimal("100"),
            "hedgeMode": "units",
        }
        initial = server.HypState("NEAR", 3, Decimal("100"), Decimal("100"), Decimal("-10"), 0, entry_price=Decimal("100"))
        falling = server.HypState("NEAR", 3, Decimal("99"), Decimal("99"), Decimal("-10"), 0, entry_price=Decimal("100"))
        deeper = server.HypState("NEAR", 3, Decimal("98.5"), Decimal("98.5"), Decimal("-10"), 0, entry_price=Decimal("99"))
        recovering = server.HypState("NEAR", 3, Decimal("99"), Decimal("99"), Decimal("-10"), 0, entry_price=Decimal("100"))
        snapshots = [
            (position, initial, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
            (position, falling, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, deeper, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, recovering, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "1", "lotMinHoldSeconds": "0"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, False, True]
            ), patch.object(
                server, "target_at_reference_price", side_effect=[Decimal("12"), Decimal("12"), Decimal("10")]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)
            self.assertEqual(server.MONITOR.state["snapshot"]["openLotCount"], 1)
            self.assertTrue(server.MONITOR.state["snapshot"]["recoveryActive"])
            self.assertEqual(server.MONITOR.state["snapshot"]["intentionalRecoveryRelease"], 1.5)
            self.assertIn("lot-recovery", events)
        finally:
            server.MONITOR.config = original

    def test_incremental_lot_does_not_close_before_minimum_time(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "NEAR", "currentPrice": Decimal("100"),
            "hedgeMode": "units",
        }
        initial = server.HypState("NEAR", 3, Decimal("100"), Decimal("100"), Decimal("-10"), 0, entry_price=Decimal("100"))
        falling = server.HypState("NEAR", 3, Decimal("99"), Decimal("99"), Decimal("-10"), 0, entry_price=Decimal("100"))
        deeper = server.HypState("NEAR", 3, Decimal("98.5"), Decimal("98.5"), Decimal("-10"), 0, entry_price=Decimal("99"))
        recovering = server.HypState("NEAR", 3, Decimal("99"), Decimal("99"), Decimal("-10"), 0, entry_price=Decimal("100"))
        snapshots = [
            (position, initial, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
            (position, falling, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, deeper, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, recovering, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {
                **original, "hedgeStrategy": "upside", "stepPercent": "1", "lotMinHoldSeconds": "60"
            }
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, False, True]
            ), patch.object(
                server, "target_at_reference_price", side_effect=[Decimal("12"), Decimal("12"), Decimal("10")]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)

            self.assertEqual(server.MONITOR.state["snapshot"]["openLotCount"], 1)
            self.assertNotIn("lot-recovery", events)
        finally:
            server.MONITOR.config = original

    def test_return_to_initial_reference_closes_entire_short_even_with_open_lot(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "NEAR", "currentPrice": Decimal("100"),
            "hedgeMode": "units",
        }
        initial = server.HypState("NEAR", 3, Decimal("100"), Decimal("100"), Decimal("-10"), 0, entry_price=Decimal("100"))
        falling = server.HypState("NEAR", 3, Decimal("99"), Decimal("99"), Decimal("-10"), 0, entry_price=Decimal("100"))
        deeper = server.HypState("NEAR", 3, Decimal("98.5"), Decimal("98.5"), Decimal("-10"), 0, entry_price=Decimal("99"))
        at_reference = server.HypState("NEAR", 3, Decimal("100"), Decimal("100"), Decimal("-10"), 0, entry_price=Decimal("100"))
        snapshots = [
            (position, initial, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
            (position, falling, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, deeper, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("12")),
            (position, at_reference, Decimal("80"), Decimal("120"), Decimal("60"), Decimal("10")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {
                **original, "hedgeStrategy": "upside", "stepPercent": "1", "lotMinHoldSeconds": "600"
            }
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, False, True]
            ), patch.object(
                server, "target_at_reference_price", side_effect=[Decimal("12"), Decimal("12"), Decimal("10")]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)

            snapshot = server.MONITOR.state["snapshot"]
            self.assertEqual(snapshot["virtualShort"], 0)
            self.assertEqual(snapshot["openLotCount"], 0)
            self.assertEqual(snapshot["hedgeRegime"], "upside")
            self.assertFalse(snapshot["recoveryActive"])
            self.assertIn("upside-close", events)
        finally:
            server.MONITOR.config = original

    def test_strategy_state_reconciles_against_real_hyperliquid_position(self):
        monitor = server.NeutralisMonitor("reconciliation")
        monitor.config = {
            **monitor.config,
            "source": "orca",
            "hyperliquidAccount": "0x1111111111111111111111111111111111111111",
            "hedgeStrategy": "upside",
        }
        position = {"positionAddress": "actual-position"}
        monitor.persisted_strategy = {
            "version": 1,
            "source": "orca",
            "positionAddress": "actual-position",
            "market": "ZEC",
            "hyperliquidAccount": monitor.config["hyperliquidAccount"],
            "hedgeStrategy": "upside",
            "hedgeRegime": "upside",
            "protectionReference": "42.75",
            "realShort": "0",
        }
        hyp = server.HypState(
            "ZEC", 3, Decimal("41"), Decimal("41"), Decimal("-2"), 0, entry_price=Decimal("41.5")
        )
        events = []
        with patch.object(monitor, "_event", side_effect=lambda event, message, **details: events.append(event)):
            restored = monitor._restore_strategy_state(position, hyp, "upside")

        self.assertIsNone(restored)
        self.assertIn("state-reconciliation", events)

    def test_dry_run_keeps_base_short_on_small_recovery_below_reference(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "CRCL", "currentPrice": Decimal("80"),
            "hedgeMode": "units",
        }
        initial = server.HypState("xyz:CRCL", 3, Decimal("80"), Decimal("80"), Decimal("-30"), 0, "xyz", Decimal("80"))
        falling = server.HypState("xyz:CRCL", 3, Decimal("79.90"), Decimal("79.90"), Decimal("-30"), 0, "xyz", Decimal("80"))
        recovered = server.HypState("xyz:CRCL", 3, Decimal("79.93"), Decimal("79.93"), Decimal("-30"), 0, "xyz", Decimal("80"))
        snapshots = [
            (position, initial, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
            (position, falling, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
            (position, recovered, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("29")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "0.5"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, True]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)
            self.assertEqual(server.MONITOR.state["snapshot"]["virtualShort"], 30)
            self.assertEqual(server.MONITOR.state["snapshot"]["hedgeRegime"], "protected")
            self.assertFalse(server.MONITOR.state["snapshot"]["recoveryActive"])
            self.assertNotIn("upside-close", events)
        finally:
            server.MONITOR.config = original

    def test_base_short_does_not_reopen_from_local_high_above_initial_reference(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "CRCL", "currentPrice": Decimal("80"),
            "hedgeMode": "units",
        }
        initial = server.HypState("xyz:CRCL", 3, Decimal("78"), Decimal("78"), Decimal("-30"), 0, "xyz", Decimal("80"))
        recovered = server.HypState("xyz:CRCL", 3, Decimal("79.93"), Decimal("79.93"), Decimal("-30"), 0, "xyz", Decimal("80"))
        recovery_high = server.HypState("xyz:CRCL", 3, Decimal("81"), Decimal("81"), Decimal("-30"), 0, "xyz", Decimal("80"))
        reversed_mark = server.HypState("xyz:CRCL", 3, Decimal("80.59"), Decimal("80.59"), Decimal("-30"), 0, "xyz", Decimal("80"))
        snapshots = [
            (position, initial, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
            (position, recovered, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("29")),
            (position, recovery_high, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("29")),
            (position, reversed_mark, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("29")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "1"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait",
                side_effect=[False, False, False, True],
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)
            self.assertEqual(server.MONITOR.state["snapshot"]["virtualShort"], 0)
            self.assertEqual(server.MONITOR.state["snapshot"]["hedgeRegime"], "upside")
            self.assertFalse(server.MONITOR.state["snapshot"]["recoveryActive"])
            self.assertNotIn("base-reentry", events)
        finally:
            server.MONITOR.config = original

    def test_protected_short_rebalances_downward_when_trigger_is_crossed(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "NEAR", "currentPrice": Decimal("4.25"),
            "hedgeMode": "units",
        }
        initial = server.HypState("NEAR", 3, Decimal("4.25"), Decimal("4.25"), Decimal("-1236.7"), 0, entry_price=Decimal("4.25"))
        falling = server.HypState("NEAR", 3, Decimal("4.07"), Decimal("4.07"), Decimal("-1236.7"), 0, entry_price=Decimal("4.25"))
        snapshots = [
            (position, initial, Decimal("3.86"), Decimal("4.56"), Decimal("60"), Decimal("1236.7")),
            (position, falling, Decimal("3.86"), Decimal("4.56"), Decimal("60"), Decimal("2047.241")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "2.5"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, True]
            ), patch.object(
                server, "target_at_reference_price", return_value=Decimal("2047.241")
            ), patch.object(
                server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)
            ):
                server.MONITOR._run(live=False)
            self.assertEqual(server.MONITOR.state["snapshot"]["virtualShort"], 2047.241)
            self.assertIn("adjustment", events)
        finally:
            server.MONITOR.config = original

    def test_target_deficit_opens_lot_before_price_trigger(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "SPCX", "currentPrice": Decimal("153.37"),
            "hedgeMode": "units",
        }
        initial = server.HypState("xyz:SPCX", 3, Decimal("153.37"), Decimal("153.37"), Decimal("-76.77"), 0, entry_price=Decimal("153.37"))
        small_drop = server.HypState("xyz:SPCX", 3, Decimal("153.03"), Decimal("153.03"), Decimal("-76.77"), 0, entry_price=Decimal("153.37"))
        snapshots = [
            (position, initial, Decimal("148.44"), Decimal("157.77"), Decimal("60"), Decimal("76.77")),
            (position, small_drop, Decimal("148.44"), Decimal("157.77"), Decimal("60"), Decimal("119.367")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "0.5"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, True]
            ), patch.object(
                server, "target_at_reference_price", return_value=Decimal("119.367")
            ), patch.object(
                server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)
            ):
                server.MONITOR._run(live=False)
            self.assertEqual(server.MONITOR.state["snapshot"]["virtualShort"], 119.367)
            self.assertEqual(server.MONITOR.state["snapshot"]["openLotCount"], 1)
            self.assertIn("target-deficit", events)
            self.assertIn("adjustment", events)
        finally:
            server.MONITOR.config = original

    def test_target_deficit_never_authorizes_short_reduction(self):
        self.assertEqual(server.target_short_deficit_ratio(Decimal("100"), Decimal("90")), Decimal("0.1"))
        self.assertEqual(server.target_short_deficit_ratio(Decimal("100"), Decimal("110")), Decimal("0"))

    def test_displayed_open_threshold_uses_recovery_reentry_level(self):
        self.assertEqual(
            server.hedge_open_threshold(
                "protected", Decimal("233.07"), Decimal("0.0075"), True, Decimal("227.82")
            ),
            Decimal("226.965675"),
        )
        self.assertEqual(
            server.hedge_open_threshold("initial_wait", Decimal("233.07"), Decimal("0.0075")),
            Decimal("232.1959875"),
        )

    def test_target_deficit_overrides_recovery_wait_only_to_increase_short(self):
        self.assertTrue(
            server.target_deficit_adjustment_allowed(
                Decimal("81.958"), Decimal("67.270"), True, False
            )
        )
        self.assertFalse(
            server.target_deficit_adjustment_allowed(
                Decimal("100"), Decimal("91"), True, False
            )
        )
        self.assertFalse(
            server.target_deficit_adjustment_allowed(
                Decimal("90"), Decimal("100"), True, False
            )
        )
        self.assertFalse(
            server.target_deficit_adjustment_allowed(
                Decimal("81.958"), Decimal("67.270"), True, True
            )
        )

    def test_intentional_recovery_release_is_not_treated_as_target_deficit(self):
        self.assertEqual(
            server.target_unintended_deficit_ratio(Decimal("100"), Decimal("80"), Decimal("20")),
            Decimal("0.05"),
        )
        self.assertFalse(
            server.target_deficit_adjustment_allowed(
                Decimal("100"), Decimal("80"), True, False, Decimal("20")
            )
        )
        self.assertEqual(
            server.target_preserving_recovery_release(Decimal("120"), Decimal("20")), Decimal("102")
        )
        self.assertEqual(
            server.target_unintended_deficit_ratio(Decimal("120"), Decimal("80"), Decimal("20")),
            Decimal("22") / Decimal("120"),
        )

    def test_recovery_release_is_capped_at_fifteen_percent_of_target(self):
        self.assertEqual(server.recovery_release_reserve(Decimal("100"), Decimal("30")), Decimal("15"))
        self.assertEqual(server.recovery_release_allowance(Decimal("100"), Decimal("12")), Decimal("3"))
        self.assertEqual(server.recovery_release_allowance(Decimal("100"), Decimal("20")), Decimal("0"))
        self.assertTrue(
            server.target_deficit_adjustment_allowed(
                Decimal("94.503"), Decimal("67.270"), True, False, Decimal("24.17")
            )
        )
        self.assertTrue(
            server.target_deficit_adjustment_allowed(
                Decimal("120"), Decimal("80"), True, False, Decimal("20")
            )
        )

    def test_dry_run_reopens_full_hedge_after_two_readings_below_band(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "CRCL", "currentPrice": Decimal("80"),
            "hedgeMode": "units",
        }
        initial = server.HypState("xyz:CRCL", 3, Decimal("80"), Decimal("80"), Decimal("0"), 0)
        first = server.HypState("xyz:CRCL", 3, Decimal("79.59"), Decimal("79.59"), Decimal("0"), 0)
        second = server.HypState("xyz:CRCL", 3, Decimal("79.58"), Decimal("79.58"), Decimal("0"), 0)
        snapshots = [
            (position, initial, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
            (position, first, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
            (position, second, Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30")),
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "0.5"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, True]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)
            self.assertGreater(server.MONITOR.state["snapshot"]["virtualShort"], 0)
            self.assertEqual(server.MONITOR.state["snapshot"]["hedgeRegime"], "protected")
            self.assertIn("downside-open", events)
        finally:
            server.MONITOR.config = original

    def test_new_unhedged_pool_waits_again_at_reference_then_opens_half_step_below(self):
        position = {
            "positionAddress": "position", "hedgeSymbol": "CRCL", "currentPrice": Decimal("80"),
            "hedgeMode": "units",
        }
        marks = ["80", "80.20", "80.21", "80.01", "80.00", "79.80", "79.79", "79.78"]
        snapshots = [
            (
                position,
                server.HypState("xyz:CRCL", 3, Decimal(mark), Decimal(mark), Decimal("0"), 0),
                Decimal("50"), Decimal("150"), Decimal("60"), Decimal("30"),
            )
            for mark in marks
        ]
        original = dict(server.MONITOR.config)
        events = []
        try:
            server.MONITOR.config = {**original, "hedgeStrategy": "upside", "stepPercent": "0.5"}
            with patch.object(server.MONITOR, "_retry_snapshot", side_effect=snapshots), patch.object(
                server.MONITOR.stop_event, "wait", side_effect=[False, False, False, False, False, False, False, True]
            ), patch.object(server.MONITOR, "_event", side_effect=lambda event, message, **details: events.append(event)):
                server.MONITOR._run(live=False)
            self.assertGreater(server.MONITOR.state["snapshot"]["virtualShort"], 0)
            self.assertEqual(server.MONITOR.state["snapshot"]["hedgeRegime"], "protected")
            self.assertEqual(server.MONITOR.state["snapshot"]["protectionReference"], 80.0)
            self.assertIn("initial-upside", events)
            self.assertIn("direction-wait", events)
            self.assertIn("downside-open", events)
        finally:
            server.MONITOR.config = original


if __name__ == "__main__":
    unittest.main()
