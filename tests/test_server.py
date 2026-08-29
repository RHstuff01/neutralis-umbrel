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

    def test_orca_discovers_classic_and_token_2022_position_nfts(self):
        wallet = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
        classic = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        token_2022 = "CNJt5jfTNps9HxE6CRgefvFCTrNdYAcetJSEosaLHzq4"
        def payload(mint):
            return {"result": {"value": [{"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"amount": "1", "decimals": 0}}}}}}]}}
        with patch.object(server, "json_request", side_effect=[payload(classic), payload(token_2022)]):
            self.assertEqual(server.solana_nft_mints(wallet), sorted([classic, token_2022]))

    def test_orca_accepts_personal_position_account(self):
        position_address = "3obGz9gF9MTcvyebAofE1bS21fTA1sfV9KFBJMsfvfTK"
        nft = "6cHCWbDnkHehmYh8LcfwKTDdq9ncHGnVuTAVNAQ5kPEw"
        data = bytearray(216)
        data[:8] = server.ORCA_POSITION_DISCRIMINATOR
        data[40:72] = server.base58_decode(nft)
        decoded = {"positionAddress": nft, "personalPositionAddress": "derived"}
        with patch.object(server, "solana_account", return_value=bytes(data)), patch.object(
            server, "orca_position", return_value=decoded
        ) as decode:
            result = server.orca_position_from_address(position_address)
        decode.assert_called_once_with(nft, bytes(data))
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

    def test_orca_pool_reports_when_wallet_has_no_position(self):
        pool = "C9U2Ksk6KKWvLEeo5yUQ7Xu46X7NzeBJtd9PBfuXaUSM"
        original = server.MONITOR.config
        server.MONITOR.config = {**original, "source": "orca", "positionAddress": pool}
        try:
            with patch.object(server, "orca_position_from_address", return_value=None), patch.object(
                server, "orca_positions", return_value=[]
            ):
                with self.assertRaisesRegex(server.NeutralisError, "desta pool Orca"):
                    server.MONITOR.positions()
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

    def test_api_wallet_key_is_never_returned(self):
        key = "11" * 32
        result = server.MONITOR.save_api_key({"privateKey": key})
        self.assertEqual(result, {"configured": True})
        self.assertNotIn(key, str(server.MONITOR.public_state()))

    def test_api_wallet_rejects_invalid_key(self):
        with self.assertRaisesRegex(server.NeutralisError, "inválida"):
            server.MONITOR.save_api_key({"privateKey": "segredo"})

    def test_auto_adjustment_waits_below_hyperliquid_minimum(self):
        position = {"hedgeSymbol": "COIN"}
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
        position = {"hedgeSymbol": "COIN"}
        hyp = server.HypState("xyz:COIN", 3, Decimal("176"), Decimal("176"), Decimal("-2.740"), 0)
        with self.assertRaisesRegex(server.NeutralisError, "US\$ 600"):
            server.MONITOR._execute_auto_adjustment(position, hyp, Decimal("3.500"))

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
        self.assertEqual(server.MONITOR.state["snapshot"]["anchor"], 176.1)
        self.assertEqual(server.MONITOR.state["snapshot"]["realShort"], 2.6)


if __name__ == "__main__":
    unittest.main()
