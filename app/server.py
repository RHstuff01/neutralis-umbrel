#!/usr/bin/env python3
"""Neutralis Umbrel: monitor de hedge dinâmico em dry-run ou modo real."""

from __future__ import annotations

import json
import base64
import hashlib
import math
import os
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


DATA_DIR = Path(os.environ.get("NEUTRALIS_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "events.jsonl"
API_KEY_FILE = DATA_DIR / "hyperliquid-api-wallet.key"
BYREAL_URL = "https://api2.byreal.io/byreal/api/dex/v2/position/list"
BYREAL_MINT_LIST_URL = "https://api2.byreal.io/byreal/api/dex/v2/mint/list"
HYP_INFO_URL = "https://api.hyperliquid.xyz/info"
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
RAYDIUM_MINT_URL = "https://api-v3.raydium.io/mint/ids"
ORCA_TOKEN_URL = "https://api.orca.so/v2/solana/tokens"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ORCA_POSITION_DISCRIMINATOR = bytes.fromhex("aabc8fe47a40f7d0")
ORCA_WHIRLPOOL_DISCRIMINATOR = bytes.fromhex("3f95d10ce1806309")
DEFAULT_SOLANA_WALLET = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
DEFAULT_HYP_ACCOUNT = "0x622dF631Bb769123FC7b8FEd0d2C363045aceDCF"
LIVE_SLIPPAGE = Decimal("0.003")
AUTO_STEP = Decimal("0.005")
AUTO_MAX_POSITION_NOTIONAL = Decimal("600")
AUTO_MIN_ORDER_NOTIONAL = Decimal("10")
AUTO_DIVERGENCE_LIMIT = Decimal("0.0075")
AUTO_POLL_SECONDS = 2
AUTO_RETRY_SECONDS = 1
AUTO_MAX_EXECUTION_ATTEMPTS = 3
AUTO_RETRY_SLIPPAGES = (Decimal("0.003"), Decimal("0.005"), Decimal("0.0075"))
PRIVATE_KEY_PATTERN = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
SOLANA_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,15}$")
STABLE_SYMBOLS = {"USD", "USDC", "USDT", "USDS", "PYUSD"}
SYMBOL_ALIASES = {"AAPLX": "AAPL", "CRCLX": "CRCL", "COINX": "COIN"}
KNOWN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1": "CRCLX",
}
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class NeutralisError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_data_permissions() -> None:
    """Prepara o volume persistente antes de iniciar o servidor."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA_DIR, 0o700)
    for path in (CONFIG_FILE, LOG_FILE):
        if path.exists():
            os.chmod(path, 0o600)


def decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise NeutralisError(f"Campo inválido: {field}") from error
    if not result.is_finite():
        raise NeutralisError(f"Campo inválido: {field}")
    return result


def optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def json_request(url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"accept": "application/json", "content-type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except Exception as error:
        raise NeutralisError(f"Falha de rede ao consultar {urlparse(url).hostname}") from error


def hyp_symbol(lp_symbol: str) -> str:
    symbol = str(lp_symbol or "").strip().upper()
    symbol = SYMBOL_ALIASES.get(symbol, symbol)
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise NeutralisError("Símbolo incompatível com a Hyperliquid")
    return symbol


def base58_decode(value: str) -> bytes:
    number = 0
    for character in value:
        try:
            digit = BASE58_ALPHABET.index(character)
        except ValueError as error:
            raise NeutralisError("Endereço Solana inválido") from error
        number = number * 58 + digit
    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + payload


def base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + (encoded or "")


def is_ed25519_point(value: bytes) -> bool:
    if len(value) != 32:
        return False
    prime = 2**255 - 19
    y = int.from_bytes(value, "little") & ((1 << 255) - 1)
    if y >= prime:
        return False
    y_squared = y * y % prime
    d = -121665 * pow(121666, prime - 2, prime) % prime
    denominator = (d * y_squared + 1) % prime
    if denominator == 0:
        return False
    x_squared = (y_squared - 1) * pow(denominator, prime - 2, prime) % prime
    return x_squared == 0 or pow(x_squared, (prime - 1) // 2, prime) == 1


def position_pda(nft_mint: str, program_id: str) -> str:
    mint = base58_decode(nft_mint)
    program = base58_decode(program_id)
    if len(mint) != 32 or len(program) != 32:
        raise NeutralisError("NFT da posição inválido")
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"position" + mint + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_ed25519_point(digest):
            return base58_encode(digest)
    raise NeutralisError("Não foi possível derivar a posição")


def raydium_position_pda(nft_mint: str) -> str:
    return position_pda(nft_mint, RAYDIUM_CLMM_PROGRAM)


def orca_position_pda(nft_mint: str) -> str:
    return position_pda(nft_mint, ORCA_WHIRLPOOL_PROGRAM)


def solana_account(address: str, expected_owner: str | None = None) -> bytes:
    response = json_request(SOLANA_RPC_URL, {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [address, {"encoding": "base64", "commitment": "confirmed"}],
    })
    value = response.get("result", {}).get("value") if isinstance(response, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise NeutralisError("Conta Solana não encontrada")
    if expected_owner and value.get("owner") != expected_owner:
        raise NeutralisError("Conta Solana pertence a um programa inesperado")
    try:
        return base64.b64decode(value["data"][0], validate=True)
    except Exception as error:
        raise NeutralisError("Resposta inválida da rede Solana") from error


def public_key_at(data: bytes, offset: int) -> str:
    value = data[offset : offset + 32]
    if len(value) != 32:
        raise NeutralisError("Conta Solana incompleta")
    return base58_encode(value)


def solana_accounts(addresses: list[str], expected_owner: str | None = None) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for start in range(0, len(addresses), 100):
        chunk = addresses[start : start + 100]
        response = json_request(SOLANA_RPC_URL, {
            "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
            "params": [chunk, {"encoding": "base64", "commitment": "confirmed"}],
        })
        values = response.get("result", {}).get("value") if isinstance(response, dict) else None
        if not isinstance(values, list) or len(values) != len(chunk):
            raise NeutralisError("Resposta incompleta da rede Solana")
        for address, value in zip(chunk, values):
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                continue
            if expected_owner and value.get("owner") != expected_owner:
                continue
            try:
                result[address] = base64.b64decode(value["data"][0], validate=True)
            except Exception as error:
                raise NeutralisError("Resposta inválida da rede Solana") from error
    return result


def raydium_symbols(mints: list[str]) -> dict[str, str]:
    symbols = {mint: KNOWN_MINTS[mint] for mint in mints if mint in KNOWN_MINTS}
    missing = [mint for mint in mints if mint not in symbols]
    if not missing:
        return symbols
    try:
        root = json_request(RAYDIUM_MINT_URL + "?" + urlencode({"mints": ",".join(missing)}))
        rows = root.get("data", root) if isinstance(root, dict) else root
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                address = str(row.get("address") or row.get("mint") or "")
                symbol = str(row.get("symbol") or "").upper()
                if address in missing and symbol:
                    symbols[address] = symbol
    except NeutralisError:
        pass
    return symbols


def orca_symbols(mints: list[str]) -> dict[str, str]:
    symbols = {mint: KNOWN_MINTS[mint] for mint in mints if mint in KNOWN_MINTS}
    missing = [mint for mint in mints if mint not in symbols]
    if missing:
        try:
            root = json_request(ORCA_TOKEN_URL + "?" + urlencode({"tokens": ",".join(missing), "size": len(missing)}))
            rows = root.get("data", []) if isinstance(root, dict) else []
            for row in rows if isinstance(rows, list) else []:
                address = str(row.get("address") or "") if isinstance(row, dict) else ""
                symbol = str(row.get("symbol") or "").upper() if isinstance(row, dict) else ""
                if address in missing and symbol:
                    symbols[address] = symbol
        except NeutralisError:
            pass
    unresolved = [mint for mint in mints if mint not in symbols]
    if unresolved:
        symbols.update(raydium_symbols(unresolved))
    return symbols


def raydium_position(nft_mint: str) -> dict[str, Any]:
    if not SOLANA_PATTERN.fullmatch(nft_mint):
        raise NeutralisError("NFT da posição Raydium inválido")
    position_address = raydium_position_pda(nft_mint)
    position_data = solana_account(position_address)
    if len(position_data) < 97:
        raise NeutralisError("Conta da posição Raydium incompleta")
    stored_nft = public_key_at(position_data, 9)
    if stored_nft != nft_mint:
        raise NeutralisError("O NFT não corresponde à posição Raydium")
    pool_address = public_key_at(position_data, 41)
    tick_lower = int.from_bytes(position_data[73:77], "little", signed=True)
    tick_upper = int.from_bytes(position_data[77:81], "little", signed=True)
    raw_liquidity = int.from_bytes(position_data[81:97], "little")
    if not (-887272 <= tick_lower < tick_upper <= 887272):
        raise NeutralisError("Faixa de ticks inválida na posição Raydium")
    if raw_liquidity <= 0:
        raise NeutralisError("A posição Raydium está sem liquidez")

    pool_data = solana_account(pool_address)
    if len(pool_data) < 273:
        raise NeutralisError("Conta do pool Raydium incompleta")
    mint_a, mint_b = public_key_at(pool_data, 73), public_key_at(pool_data, 105)
    decimals_a, decimals_b = pool_data[233], pool_data[234]
    sqrt_price_x64 = int.from_bytes(pool_data[253:269], "little")
    if sqrt_price_x64 <= 0:
        raise NeutralisError("Preço inválido no pool Raydium")
    symbols = raydium_symbols([mint_a, mint_b])
    symbol_a, symbol_b = symbols.get(mint_a, ""), symbols.get(mint_b, "")
    stable_a, stable_b = symbol_a in STABLE_SYMBOLS, symbol_b in STABLE_SYMBOLS
    if stable_a == stable_b:
        raise NeutralisError("A LP Raydium precisa ter um ativo e uma cotação estável reconhecida")

    scale = 10 ** (decimals_a - decimals_b)
    raw_price = (sqrt_price_x64 / 2**64) ** 2
    price_b_per_a = raw_price * scale
    tick_lower_price = (1.0001**tick_lower) * scale
    tick_upper_price = (1.0001**tick_upper) * scale
    sqrt_current, sqrt_lower, sqrt_upper = math.sqrt(raw_price), math.sqrt((1.0001**tick_lower)), math.sqrt((1.0001**tick_upper))
    if sqrt_current <= sqrt_lower:
        amount_a_raw = raw_liquidity * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
        amount_b_raw = 0.0
    elif sqrt_current >= sqrt_upper:
        amount_a_raw = 0.0
        amount_b_raw = raw_liquidity * (sqrt_upper - sqrt_lower)
    else:
        amount_a_raw = raw_liquidity * (sqrt_upper - sqrt_current) / (sqrt_current * sqrt_upper)
        amount_b_raw = raw_liquidity * (sqrt_current - sqrt_lower)
    amount_a, amount_b = amount_a_raw / 10**decimals_a, amount_b_raw / 10**decimals_b

    if stable_b:
        asset_symbol, quote_symbol = symbol_a, symbol_b
        lower_price, upper_price, current_price = tick_lower_price, tick_upper_price, price_b_per_a
        asset_amount, quote_amount = amount_a, amount_b
    else:
        asset_symbol, quote_symbol = symbol_b, symbol_a
        lower_price, upper_price, current_price = 1 / tick_upper_price, 1 / tick_lower_price, 1 / price_b_per_a
        asset_amount, quote_amount = amount_b, amount_a
    liquidity_usd = asset_amount * current_price + quote_amount
    normalized_liquidity = raw_liquidity / (10 ** ((decimals_a + decimals_b) / 2))
    return {
        "source": "raydium",
        "positionAddress": nft_mint,
        "personalPositionAddress": position_address,
        "poolAddress": pool_address,
        "pair": f"{asset_symbol} / {quote_symbol}",
        "assetSymbol": asset_symbol,
        "hedgeSymbol": hyp_symbol(asset_symbol),
        "quoteSymbol": quote_symbol,
        "liquidityUsd": liquidity_usd,
        "lowerPrice": lower_price,
        "upperPrice": upper_price,
        "currentPrice": current_price,
        "assetAmount": asset_amount,
        "quoteAmount": quote_amount,
        "normalizedLiquidity": normalized_liquidity,
        "basisWarning": asset_symbol != hyp_symbol(asset_symbol),
        "importable": bool(asset_symbol and 0 < lower_price < upper_price and raw_liquidity > 0),
    }


def solana_nft_mints(wallet: str) -> list[str]:
    if not SOLANA_PATTERN.fullmatch(wallet):
        raise NeutralisError("Carteira Solana inválida")
    mints: set[str] = set()
    successful_queries = 0
    for program_id in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            response = json_request(SOLANA_RPC_URL, {
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [wallet, {"programId": program_id}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            })
        except NeutralisError:
            continue
        successful_queries += 1
        rows = response.get("result", {}).get("value") if isinstance(response, dict) else None
        for row in rows if isinstance(rows, list) else []:
            info = row.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}) if isinstance(row, dict) else {}
            amount = info.get("tokenAmount", {}) if isinstance(info, dict) else {}
            mint = str(info.get("mint") or "") if isinstance(info, dict) else ""
            if amount.get("decimals") == 0 and str(amount.get("amount")) == "1" and SOLANA_PATTERN.fullmatch(mint):
                mints.add(mint)
    if not successful_queries:
        raise NeutralisError("Falha ao consultar os NFTs da carteira Solana")
    return sorted(mints)


def orca_position(nft_mint: str, position_data: bytes | None = None) -> dict[str, Any]:
    if not SOLANA_PATTERN.fullmatch(nft_mint):
        raise NeutralisError("NFT da posição Orca inválido")
    position_address = orca_position_pda(nft_mint)
    data = position_data if position_data is not None else solana_account(position_address, ORCA_WHIRLPOOL_PROGRAM)
    if len(data) < 96 or data[:8] != ORCA_POSITION_DISCRIMINATOR:
        raise NeutralisError("Conta da posição Orca inválida")
    pool_address = public_key_at(data, 8)
    stored_nft = public_key_at(data, 40)
    raw_liquidity = int.from_bytes(data[72:88], "little")
    tick_lower = int.from_bytes(data[88:92], "little", signed=True)
    tick_upper = int.from_bytes(data[92:96], "little", signed=True)
    if stored_nft != nft_mint:
        raise NeutralisError("O NFT não corresponde à posição Orca")
    if not (-443636 <= tick_lower < tick_upper <= 443636):
        raise NeutralisError("Faixa de ticks inválida na posição Orca")
    if raw_liquidity <= 0:
        raise NeutralisError("A posição Orca está sem liquidez")

    pool_data = solana_account(pool_address, ORCA_WHIRLPOOL_PROGRAM)
    if len(pool_data) < 213:
        raise NeutralisError("Conta do Whirlpool Orca incompleta")
    sqrt_price_x64 = int.from_bytes(pool_data[65:81], "little")
    mint_a, mint_b = public_key_at(pool_data, 101), public_key_at(pool_data, 181)
    if sqrt_price_x64 <= 0:
        raise NeutralisError("Preço inválido no Whirlpool Orca")
    mint_a_data, mint_b_data = solana_account(mint_a), solana_account(mint_b)
    if len(mint_a_data) < 45 or len(mint_b_data) < 45:
        raise NeutralisError("Mint da posição Orca incompleto")
    decimals_a, decimals_b = mint_a_data[44], mint_b_data[44]
    symbols = orca_symbols([mint_a, mint_b])
    symbol_a, symbol_b = symbols.get(mint_a, ""), symbols.get(mint_b, "")
    stable_a, stable_b = symbol_a in STABLE_SYMBOLS, symbol_b in STABLE_SYMBOLS
    if stable_a == stable_b:
        raise NeutralisError("A LP Orca precisa ter um ativo e uma cotação estável reconhecida")

    scale = 10 ** (decimals_a - decimals_b)
    raw_price = (sqrt_price_x64 / 2**64) ** 2
    price_b_per_a = raw_price * scale
    tick_lower_price = (1.0001**tick_lower) * scale
    tick_upper_price = (1.0001**tick_upper) * scale
    sqrt_current, sqrt_lower, sqrt_upper = math.sqrt(raw_price), math.sqrt(1.0001**tick_lower), math.sqrt(1.0001**tick_upper)
    if sqrt_current <= sqrt_lower:
        amount_a_raw = raw_liquidity * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
        amount_b_raw = 0.0
    elif sqrt_current >= sqrt_upper:
        amount_a_raw = 0.0
        amount_b_raw = raw_liquidity * (sqrt_upper - sqrt_lower)
    else:
        amount_a_raw = raw_liquidity * (sqrt_upper - sqrt_current) / (sqrt_current * sqrt_upper)
        amount_b_raw = raw_liquidity * (sqrt_current - sqrt_lower)
    amount_a, amount_b = amount_a_raw / 10**decimals_a, amount_b_raw / 10**decimals_b

    if stable_b:
        asset_symbol, quote_symbol = symbol_a, symbol_b
        lower_price, upper_price, current_price = tick_lower_price, tick_upper_price, price_b_per_a
        asset_amount, quote_amount = amount_a, amount_b
    else:
        asset_symbol, quote_symbol = symbol_b, symbol_a
        lower_price, upper_price, current_price = 1 / tick_upper_price, 1 / tick_lower_price, 1 / price_b_per_a
        asset_amount, quote_amount = amount_b, amount_a
    liquidity_usd = asset_amount * current_price + quote_amount
    normalized_liquidity = raw_liquidity / (10 ** ((decimals_a + decimals_b) / 2))
    return {
        "source": "orca",
        "positionAddress": nft_mint,
        "personalPositionAddress": position_address,
        "poolAddress": pool_address,
        "pair": f"{asset_symbol} / {quote_symbol}",
        "assetSymbol": asset_symbol,
        "hedgeSymbol": hyp_symbol(asset_symbol),
        "quoteSymbol": quote_symbol,
        "liquidityUsd": liquidity_usd,
        "lowerPrice": lower_price,
        "upperPrice": upper_price,
        "currentPrice": current_price,
        "assetAmount": asset_amount,
        "quoteAmount": quote_amount,
        "normalizedLiquidity": normalized_liquidity,
        "basisWarning": asset_symbol != hyp_symbol(asset_symbol),
        "importable": bool(asset_symbol and 0 < lower_price < upper_price and raw_liquidity > 0),
    }


def orca_position_from_address(address: str) -> dict[str, Any] | None:
    """Aceita tanto o mint do NFT quanto a conta Position do Whirlpool."""
    if not SOLANA_PATTERN.fullmatch(address):
        raise NeutralisError("Endereço da posição Orca inválido")
    account_data = solana_account(address)
    if len(account_data) >= 96 and account_data[:8] == ORCA_POSITION_DISCRIMINATOR:
        nft_mint = public_key_at(account_data, 40)
        result = orca_position(nft_mint, account_data)
        result["personalPositionAddress"] = address
        return result
    if len(account_data) >= 8 and account_data[:8] == ORCA_WHIRLPOOL_DISCRIMINATOR:
        return None
    return orca_position(address)


def orca_positions(wallet: str) -> list[dict[str, Any]]:
    nft_mints = solana_nft_mints(wallet)
    derived = {orca_position_pda(mint): mint for mint in nft_mints}
    accounts = solana_accounts(list(derived), ORCA_WHIRLPOOL_PROGRAM)
    positions = []
    for address, data in accounts.items():
        if len(data) < 96 or data[:8] != ORCA_POSITION_DISCRIMINATOR:
            continue
        try:
            positions.append(orca_position(derived[address], data))
        except NeutralisError:
            continue
    return positions


def token_metadata(pool: dict[str, Any], side: str) -> dict[str, Any]:
    direct = pool.get(f"mint{side}")
    candidates = [direct, pool.get(f"mint{side}Info"), pool.get(f"token{side}"), pool.get(f"token{side}Info")]
    value = next((item for item in candidates if isinstance(item, dict)), {})
    return {
        "address": direct if isinstance(direct, str) else str(value.get("address") or value.get("mintAddress") or value.get("mint") or ""),
        "symbol": str(value.get("symbol") or value.get("ticker") or "").upper(),
        "decimals": optional_float(value.get("decimals", value.get("decimal"))),
        "priceUsd": optional_float(value.get("priceUsd", value.get("usdPrice"))),
    }


def normalize_position(position: dict[str, Any], pool: dict[str, Any]) -> dict[str, Any]:
    token_a = token_metadata(pool, "A")
    token_b = token_metadata(pool, "B")
    stable_a = token_a["symbol"] in STABLE_SYMBOLS
    stable_b = token_b["symbol"] in STABLE_SYMBOLS
    asset = token_b if stable_a and not stable_b else token_a
    quote = token_b if stable_b and not stable_a else token_a
    lower_tick = optional_float(position.get("lowerTick", position.get("tickLower")))
    upper_tick = optional_float(position.get("upperTick", position.get("tickUpper")))
    value_usd = optional_float(position.get("liquidityUsd", position.get("positionValueUsd", position.get("valueUsd"))))
    lower_price = None
    upper_price = None
    current_price = None
    if stable_a != stable_b and token_a["decimals"] is not None and token_b["decimals"] is not None and lower_tick is not None and upper_tick is not None:
        scale = 10 ** (token_a["decimals"] - token_b["decimals"])
        tick_lower_price = (1.0001**lower_tick) * scale
        tick_upper_price = (1.0001**upper_tick) * scale
        current_tick = optional_float(pool.get("tickCurrent", pool.get("currentTick")))
        tick_current_price = (1.0001**current_tick) * scale if current_tick is not None else None
        if stable_b:
            lower_price, upper_price = tick_lower_price, tick_upper_price
            current_price = tick_current_price
        else:
            lower_price, upper_price = 1 / tick_upper_price, 1 / tick_lower_price
            current_price = 1 / tick_current_price if tick_current_price else None
    if current_price is None and asset.get("priceUsd") is not None:
        quote_usd = quote.get("priceUsd") or 1.0
        if quote_usd > 0:
            current_price = asset["priceUsd"] / quote_usd
    importable = bool(
        asset["symbol"]
        and quote["symbol"]
        and value_usd is not None
        and value_usd > 0
        and lower_price is not None
        and upper_price is not None
        and math.isfinite(lower_price)
        and math.isfinite(upper_price)
        and 0 < lower_price < upper_price
    )
    return {
        "positionAddress": str(position.get("positionAddress") or position.get("address") or ""),
        "poolAddress": str(position.get("poolAddress") or pool.get("poolAddress") or ""),
        "pair": f'{asset["symbol"]} / {quote["symbol"]}' if asset["symbol"] and quote["symbol"] else "Pool Byreal",
        "assetSymbol": asset["symbol"],
        "assetMint": asset["address"],
        "hedgeSymbol": hyp_symbol(asset["symbol"]) if asset["symbol"] else "",
        "quoteSymbol": quote["symbol"],
        "quoteMint": quote["address"],
        "liquidityUsd": value_usd,
        "lowerPrice": lower_price,
        "upperPrice": upper_price,
        "currentPrice": current_price,
        "earnedUsd": optional_float(position.get("earnedUsd")),
        "apr": optional_float(position.get("apr")),
        "importable": importable,
    }


def byreal_mint_price(mint: str) -> float | None:
    """Obtém preço USD apenas quando a resposta corresponde ao mint solicitado."""
    if not SOLANA_PATTERN.fullmatch(mint):
        return None
    root = json_request(BYREAL_MINT_LIST_URL + "?" + urlencode({
        "page": 1,
        "pageSize": 10,
        "search": mint,
    }))

    def visit(value: Any) -> float | None:
        if isinstance(value, dict):
            address = str(value.get("mintAddress") or value.get("address") or value.get("mint") or "")
            if address == mint:
                price = optional_float(value.get("priceUsd", value.get("usdPrice")))
                if price is not None and price > 0:
                    return price
            for nested in value.values():
                found = visit(nested)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = visit(nested)
                if found is not None:
                    return found
        return None

    return visit(root)


def byreal_positions(wallet: str) -> list[dict[str, Any]]:
    if not SOLANA_PATTERN.fullmatch(wallet):
        raise NeutralisError("Carteira Solana inválida")
    query = f"?userAddress={wallet}&status=0&page=1&pageSize=100"
    root = json_request(BYREAL_URL + query)
    data = root.get("result", {}).get("data") if isinstance(root, dict) and isinstance(root.get("result"), dict) else None
    if data is None and isinstance(root, dict):
        data = root.get("data", root.get("result", root))
    data = data if isinstance(data, dict) else {}
    rows = data.get("positions", data.get("records", []))
    rows = rows if isinstance(rows, list) else []
    pool_map = data.get("poolMap", {})
    pools = pool_map if isinstance(pool_map, list) else list(pool_map.values()) if isinstance(pool_map, dict) else []
    result = []
    for position in rows:
        if not isinstance(position, dict):
            continue
        address = str(position.get("poolAddress") or "")
        if isinstance(pool_map, dict) and isinstance(pool_map.get(address), dict):
            pool = pool_map[address]
        else:
            pool = next((item for item in pools if isinstance(item, dict) and str(item.get("poolAddress") or item.get("address") or "") == address), {})
        normalized = normalize_position(position, pool)
        if normalized["currentPrice"] is None and normalized["assetMint"]:
            try:
                asset_usd = byreal_mint_price(normalized["assetMint"])
                quote_usd = 1.0 if normalized["quoteSymbol"] in STABLE_SYMBOLS else byreal_mint_price(normalized["quoteMint"])
                if asset_usd is not None and quote_usd is not None and quote_usd > 0:
                    normalized["currentPrice"] = asset_usd / quote_usd
            except NeutralisError:
                # A listagem da posição ainda pode ser usada em dry-run; o modo
                # real permanece bloqueado sem uma cotação independente válida.
                pass
        result.append(normalized)
    return result


@dataclass
class HypState:
    market: str
    decimals: int
    mark: Decimal
    oracle: Decimal
    signed_position: Decimal
    open_orders: int


def hyp_state(account: str, symbol: str) -> HypState:
    if not EVM_PATTERN.fullmatch(account):
        raise NeutralisError("Conta Hyperliquid inválida")
    symbol = hyp_symbol(symbol)
    market = f"xyz:{symbol}"
    metadata, contexts = json_request(HYP_INFO_URL, {"type": "metaAndAssetCtxs", "dex": "xyz"})
    clearinghouse = json_request(HYP_INFO_URL, {"type": "clearinghouseState", "user": account, "dex": "xyz"})
    orders = json_request(HYP_INFO_URL, {"type": "frontendOpenOrders", "user": account, "dex": "xyz"})
    universe = metadata.get("universe", [])
    index = next((i for i, row in enumerate(universe) if str(row.get("name", "")).upper() in {symbol, market.upper()}), None)
    if index is None:
        raise NeutralisError(f"Contrato {market} não encontrado na Hyperliquid")
    context = contexts[index]
    signed = Decimal("0")
    for row in clearinghouse.get("assetPositions", []):
        position = row.get("position", {})
        if str(position.get("coin", "")).upper() in {symbol, market.upper()}:
            signed = decimal(position.get("szi", 0), "Hyperliquid szi")
            break
    open_orders = sum(1 for row in orders if str(row.get("coin", "")).upper() in {symbol, market.upper()})
    return HypState(
        market=market,
        decimals=int(universe[index]["szDecimals"]),
        mark=decimal(context.get("markPx"), "Hyperliquid markPx"),
        oracle=decimal(context.get("oraclePx", context.get("markPx")), "Hyperliquid oraclePx"),
        signed_position=signed,
        open_orders=open_orders,
    )




def lp_liquidity(value: Decimal, price: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    if not (value > 0 and Decimal("0") < lower < price < upper):
        raise NeutralisError("Preço fora da faixa válida da LP")
    sqrt_lower, sqrt_price, sqrt_upper = lower.sqrt(), price.sqrt(), upper.sqrt()
    base_per_liquidity = (sqrt_upper - sqrt_price) / (sqrt_price * sqrt_upper)
    stable_per_liquidity = sqrt_price - sqrt_lower
    return value / (base_per_liquidity * price + stable_per_liquidity)


def base_target(liquidity: Decimal, price: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    sqrt_lower, sqrt_upper = lower.sqrt(), upper.sqrt()
    if price <= lower:
        return liquidity * (Decimal("1") / sqrt_lower - Decimal("1") / sqrt_upper)
    if price >= upper:
        return Decimal("0")
    sqrt_price = price.sqrt()
    return liquidity * (Decimal("1") / sqrt_price - Decimal("1") / sqrt_upper)


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


class NeutralisMonitor:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.execution_lock = threading.Lock()
        self.config = self._load_config()
        self.state: dict[str, Any] = {
            "mode": "stopped",
            "message": "Pronto para iniciar",
            "updatedAt": now_iso(),
            "snapshot": None,
        }

    def _load_config(self) -> dict[str, str]:
        defaults = {
            "source": "byreal",
            "solanaWallet": DEFAULT_SOLANA_WALLET,
            "hyperliquidAccount": DEFAULT_HYP_ACCOUNT,
            "positionAddress": "",
        }
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update({key: str(stored.get(key, defaults[key])) for key in defaults})
        except (OSError, json.JSONDecodeError):
            pass
        return defaults

    def save_config(self, incoming: dict[str, Any]) -> dict[str, str]:
        source = str(incoming.get("source", self.config["source"])).lower()
        wallet = str(incoming.get("solanaWallet", self.config["solanaWallet"]))
        account = str(incoming.get("hyperliquidAccount", self.config["hyperliquidAccount"]))
        position = str(incoming.get("positionAddress", self.config["positionAddress"]))
        if source not in {"byreal", "raydium", "orca"}:
            raise NeutralisError("Fonte de liquidez inválida")
        if not SOLANA_PATTERN.fullmatch(wallet):
            raise NeutralisError("Carteira Solana inválida")
        if not EVM_PATTERN.fullmatch(account):
            raise NeutralisError("Conta Hyperliquid inválida")
        if position and not SOLANA_PATTERN.fullmatch(position):
            raise NeutralisError("Endereço da posição inválido")
        with self.lock:
            if self.state["mode"] == "running":
                raise NeutralisError("Pare o monitor antes de alterar a configuração")
            self.config = {"source": source, "solanaWallet": wallet, "hyperliquidAccount": account, "positionAddress": position}
            CONFIG_FILE.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
            os.chmod(CONFIG_FILE, 0o600)
            return dict(self.config)

    def save_api_key(self, incoming: dict[str, Any]) -> dict[str, Any]:
        key = str(incoming.get("privateKey", "")).strip()
        if not PRIVATE_KEY_PATTERN.fullmatch(key):
            raise NeutralisError("Chave privada da API Wallet inválida")
        normalized = key if key.startswith("0x") else f"0x{key}"
        API_KEY_FILE.write_text(normalized, encoding="ascii")
        os.chmod(API_KEY_FILE, 0o600)
        return {"configured": True}

    def _api_key(self) -> str:
        try:
            key = API_KEY_FILE.read_text(encoding="ascii").strip()
        except OSError as error:
            raise NeutralisError("Cadastre a chave da API Wallet no Umbrel") from error
        if not PRIVATE_KEY_PATTERN.fullmatch(key):
            raise NeutralisError("Chave da API Wallet armazenada é inválida")
        return key if key.startswith("0x") else f"0x{key}"

    def _exchange(self):
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils.constants import MAINNET_API_URL
        except ImportError as error:
            missing = getattr(error, "name", None) or str(error)
            raise NeutralisError(f"SDK da Hyperliquid não está disponível ({missing})") from error
        wallet = Account.from_key(self._api_key())
        return Exchange(
            wallet,
            MAINNET_API_URL,
            account_address=self.config["hyperliquidAccount"],
            perp_dexs=["xyz"],
        )

    @staticmethod
    def _order_status(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise NeutralisError("A Hyperliquid rejeitou a ordem automática")
        statuses = response.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses or not isinstance(statuses[0], dict):
            raise NeutralisError("Resposta inesperada ao enviar a ordem automática")
        status = statuses[0]
        if "error" in status:
            raise NeutralisError(f"Ordem automática rejeitada: {status['error']}")
        if "filled" not in status:
            raise NeutralisError("A ordem IOC não foi executada; monitor pausado")
        return status["filled"]

    def _execute_auto_adjustment(
        self,
        position: dict[str, Any],
        hyp: HypState,
        target: Decimal,
    ) -> dict[str, Any] | None:
        original_position = position.get("positionAddress")
        total_filled = Decimal("0")
        last_direction: bool | None = None

        for attempt in range(AUTO_MAX_EXECUTION_ATTEMPTS):
            if attempt:
                position, hyp, lower, upper, _, target = self._live_snapshot()
                if original_position and position.get("positionAddress") != original_position:
                    raise NeutralisError("A posição selecionada mudou durante o ajuste")
                if hyp.mark <= lower or hyp.mark >= upper:
                    raise NeutralisError("O preço saiu da faixa da LP durante o ajuste")
                if position.get("currentPrice") is None:
                    raise NeutralisError("A fonte deixou de fornecer o preço independente da LP")
                lp_price = decimal(position["currentPrice"], "preço da LP")
                if abs(lp_price - hyp.mark) / hyp.mark > AUTO_DIVERGENCE_LIMIT:
                    raise NeutralisError("Preço da LP e Hyperliquid divergiram mais de 0,75%")

            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            if hyp.signed_position > 0:
                raise NeutralisError("A conta ficou long; monitor pausado")
            if hyp.oracle <= 0 or abs(hyp.mark - hyp.oracle) / hyp.oracle > AUTO_DIVERGENCE_LIMIT:
                raise NeutralisError("Mark e oráculo divergiram mais de 0,75%")

            current_short = abs(min(hyp.signed_position, Decimal("0")))
            quantum = Decimal(1).scaleb(-hyp.decimals)
            difference = target - current_short
            size = abs(difference).quantize(quantum, rounding=ROUND_DOWN)
            residual_notional = abs(difference) * hyp.mark
            if size <= 0 or residual_notional < AUTO_MIN_ORDER_NOTIONAL:
                return {
                    "size": total_filled,
                    "notional": total_filled * hyp.mark,
                    "isBuy": last_direction,
                    "filled": total_filled,
                    "residualNotional": residual_notional,
                    "currentShort": current_short,
                    "target": target,
                    "anchor": hyp.mark,
                } if total_filled else None

            is_buy = difference < 0
            if is_buy:
                size = min(size, current_short)
            else:
                resulting_notional = (current_short + size) * hyp.mark
                if resulting_notional > AUTO_MAX_POSITION_NOTIONAL:
                    raise NeutralisError("Short-alvo ultrapassaria o limite total de US$ 600")
            notional = size * hyp.mark
            if notional < AUTO_MIN_ORDER_NOTIONAL:
                return None

            slippage = AUTO_RETRY_SLIPPAGES[attempt]
            limit_price = hyp.mark * (Decimal("1") + slippage if is_buy else Decimal("1") - slippage)
            limit_price = Decimal(f"{limit_price:.5g}")
            response = None
            try:
                with self.execution_lock:
                    current = hyp_state(self.config["hyperliquidAccount"], position["hedgeSymbol"])
                    if current.open_orders or current.signed_position != hyp.signed_position:
                        raise NeutralisError("A posição ou as ordens mudaram durante a validação")
                    if abs(current.mark - hyp.mark) / hyp.mark > AUTO_STEP:
                        raise NeutralisError("O preço mudou mais de 0,5% durante a validação")
                    response = self._exchange().order(
                        hyp.market,
                        is_buy,
                        float(size),
                        float(limit_price),
                        {"limit": {"tif": "Ioc"}},
                        reduce_only=is_buy,
                    )
                filled = self._order_status(response)
            except NeutralisError as error:
                retryable = "IOC" in str(error).upper() or "IOCCANCEL" in str(error).upper()
                if not retryable:
                    raise
                if attempt + 1 >= AUTO_MAX_EXECUTION_ATTEMPTS:
                    raise NeutralisError(
                        f"Hedge incompleto após {AUTO_MAX_EXECUTION_ATTEMPTS} tentativas; "
                        f"delta residual US$ {residual_notional:.2f}"
                    ) from error
                self._event(
                    "live-retry",
                    f"IOC não executada; nova tentativa {attempt + 2}/{AUTO_MAX_EXECUTION_ATTEMPTS}",
                    target=target,
                    mark=hyp.mark,
                    response=response,
                )
                if self.stop_event.wait(AUTO_RETRY_SECONDS):
                    raise NeutralisError("Monitor interrompido durante o ajuste")
                continue

            filled_size = decimal(filled.get("totalSz", size), "quantidade executada")
            total_filled += filled_size
            last_direction = is_buy
            action = "COMPRAR / reduzir short" if is_buy else "VENDER / aumentar short"
            self._event(
                "live-adjustment",
                f"ORDEM REAL {action} {size} {position['hedgeSymbol']}",
                attempt=attempt + 1,
                requestedSize=size,
                target=target,
                mark=hyp.mark,
                limitPrice=limit_price,
                reduceOnly=is_buy,
                filled=filled,
            )
            if attempt + 1 < AUTO_MAX_EXECUTION_ATTEMPTS and self.stop_event.wait(AUTO_RETRY_SECONDS):
                raise NeutralisError("Monitor interrompido durante o ajuste")

        position, hyp, _, _, _, target = self._live_snapshot()
        current_short = abs(min(hyp.signed_position, Decimal("0")))
        residual_notional = abs(target - current_short) * hyp.mark
        if residual_notional >= AUTO_MIN_ORDER_NOTIONAL:
            raise NeutralisError(
                f"Hedge incompleto após {AUTO_MAX_EXECUTION_ATTEMPTS} tentativas; "
                f"delta residual US$ {residual_notional:.2f}"
            )
        return {
            "size": total_filled,
            "notional": total_filled * hyp.mark,
            "isBuy": last_direction,
            "filled": total_filled,
            "residualNotional": residual_notional,
            "currentShort": current_short,
            "target": target,
            "anchor": hyp.mark,
        }

    def positions(self) -> list[dict[str, Any]]:
        if self.config["source"] == "raydium":
            position = self.config["positionAddress"]
            if not position:
                return []
            return [raydium_position(position)]
        if self.config["source"] == "orca":
            selected = self.config["positionAddress"]
            direct = None
            direct_error = None
            selected_pool = None
            if selected:
                try:
                    direct = orca_position_from_address(selected)
                    if direct is None:
                        selected_pool = selected
                except NeutralisError as error:
                    direct_error = error
            discovered = orca_positions(self.config["solanaWallet"])
            if selected_pool:
                discovered = [item for item in discovered if item.get("poolAddress") == selected_pool]
            if direct and all(item["positionAddress"] != direct["positionAddress"] for item in discovered):
                discovered.insert(0, direct)
            if selected and not discovered and direct_error:
                raise direct_error
            if selected_pool and not discovered:
                raise NeutralisError("Nenhuma posição aberta desta pool Orca foi encontrada na carteira configurada")
            return discovered
        return byreal_positions(self.config["solanaWallet"])

    def _selected_position(self) -> dict[str, Any]:
        positions = self.positions()
        selected = self.config["positionAddress"]
        if selected:
            position = next(
                (
                    item for item in positions
                    if selected in {
                        item["positionAddress"], item.get("personalPositionAddress"), item.get("poolAddress")
                    }
                ),
                None,
            )
        else:
            position = next((item for item in positions if item["importable"]), None)
        if not position:
            raise NeutralisError("A posição selecionada não está aberta ou não pode ser calculada")
        if not position["importable"]:
            raise NeutralisError("A fonte não forneceu dados suficientes para calcular esta LP")
        return position

    def _live_snapshot(self) -> tuple[dict[str, Any], HypState, Decimal, Decimal, Decimal, Decimal]:
        position = self._selected_position()
        hyp = hyp_state(self.config["hyperliquidAccount"], position["hedgeSymbol"])
        value = decimal(position["liquidityUsd"], "liquidityUsd")
        lower = decimal(position["lowerPrice"], "lowerPrice")
        upper = decimal(position["upperPrice"], "upperPrice")
        if position.get("normalizedLiquidity") is not None:
            liquidity = decimal(position["normalizedLiquidity"], "normalizedLiquidity")
        else:
            liquidity = lp_liquidity(value, hyp.mark, lower, upper)
        target = base_target(liquidity, hyp.mark, lower, upper)
        return position, hyp, lower, upper, liquidity, target

    def _event(self, event: str, message: str, **details: Any) -> None:
        record = {"at": now_iso(), "event": event, "message": message, **json_safe(details)}
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self.lock:
            self.state["lastEvent"] = record

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)) :]
            return [json.loads(line) for line in reversed(lines) if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    def start(self, live: bool = False, confirmation: str = "") -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise NeutralisError("O monitor já está em execução")
            if live:
                position, hyp, _, _, _, _ = self._live_snapshot()
                expected = f"ATIVAR HEDGE REAL {hyp.market.upper()} MAX US$ 600"
                if confirmation.strip().upper() != expected:
                    raise NeutralisError(f"Confirmação incorreta. Digite exatamente: {expected}")
                if not API_KEY_FILE.exists():
                    raise NeutralisError("Cadastre a chave da API Wallet antes de ativar o modo real")
                if position["hedgeSymbol"] != hyp_symbol(position["assetSymbol"]):
                    raise NeutralisError("Mapeamento do contrato não pôde ser validado")
            self.stop_event.clear()
            self.state.update({"mode": "starting", "message": "Validando fontes de dados", "updatedAt": now_iso()})
            self.thread = threading.Thread(target=self._run, args=(live,), name="neutralis-live" if live else "neutralis-dry-run", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.state.update({"mode": "stopped", "message": "Monitor interrompido pelo usuário", "updatedAt": now_iso()})
        self._event("stop", "Monitor interrompido pelo usuário")

    def _pause(self, message: str) -> None:
        with self.lock:
            self.state.update({"mode": "paused", "message": message, "updatedAt": now_iso()})
        self._event("pause", message)

    def _run(self, live: bool = False) -> None:
        step = AUTO_STEP
        divergence_limit = AUTO_DIVERGENCE_LIMIT
        try:
            position, hyp, lower, upper, liquidity, target = self._live_snapshot()
            if hyp.signed_position > 0:
                raise NeutralisError("A conta está long; o monitor exige posição zero ou short")
            if live and abs(hyp.signed_position) * hyp.mark > AUTO_MAX_POSITION_NOTIONAL:
                raise NeutralisError("O short real já ultrapassa o limite total de US$ 600")
            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            if hyp.oracle <= 0 or abs(hyp.mark - hyp.oracle) / hyp.oracle > divergence_limit:
                raise NeutralisError("Mark e oráculo divergiram mais de 0,75%")
            if live and position.get("currentPrice") is None:
                raise NeutralisError("A fonte não forneceu preço independente da LP; modo real bloqueado")
            lp_price = decimal(position.get("currentPrice") or hyp.mark, "preço da LP")
            if abs(lp_price - hyp.mark) / hyp.mark > divergence_limit:
                raise NeutralisError("Preço da LP e Hyperliquid divergiram mais de 0,75%")
            initial_adjusted = False
            current_short = abs(min(hyp.signed_position, Decimal("0")))
            initial_residual = abs(target - current_short) * hyp.mark
            if live and initial_residual >= AUTO_MIN_ORDER_NOTIONAL:
                self._event(
                    "initial-reconciliation",
                    f"CORRIGIR DELTA INICIAL · residual US$ {initial_residual:.2f}",
                    currentShort=current_short,
                    target=target,
                    mark=hyp.mark,
                )
                result = self._execute_auto_adjustment(position, hyp, target)
                initial_adjusted = bool(result)
                position, hyp, lower, upper, liquidity, target = self._live_snapshot()
                lp_price = decimal(position.get("currentPrice"), "preço da LP")
            initial_signed = hyp.signed_position
            virtual_short = abs(min(initial_signed, Decimal("0")))
            quantum = Decimal(1).scaleb(-hyp.decimals)
            anchor = hyp.mark
            initial_snapshot = {
                "position": position,
                "market": hyp.market,
                "mark": hyp.mark,
                "oracle": hyp.oracle,
                "lpPrice": lp_price,
                "basisPercent": abs(lp_price - hyp.mark) / hyp.mark * 100,
                "realShort": abs(min(hyp.signed_position, Decimal("0"))),
                "virtualShort": virtual_short,
                "targetShort": target,
                "anchor": anchor,
                "lower": lower,
                "upper": upper,
                "stepPercent": 0.5,
                "live": live,
                "pendingNotional": abs(target - virtual_short) * hyp.mark,
            }
            with self.lock:
                label = "MODO REAL ativo" if live else "Dry-run ativo"
                self.state.update({"mode": "running", "message": f"{label} · aguardando nível de 0,5%", "snapshot": json_safe(initial_snapshot), "updatedAt": now_iso()})
            start_message = (
                f"MODO REAL iniciado em {hyp.market}; delta inicial corrigido"
                if live and initial_adjusted
                else f"{'MODO REAL' if live else 'Dry-run'} iniciado em {hyp.market}; delta inicial dentro do mínimo negociável"
                if live
                else f"Dry-run iniciado em {hyp.market}; nenhuma ordem inicial"
            )
            self._event("start-live" if live else "start", start_message, mark=hyp.mark, lower=lower, upper=upper, anchor=anchor)

            while not self.stop_event.wait(AUTO_POLL_SECONDS):
                position_now, hyp_now, lower, upper, liquidity, target = self._live_snapshot()
                if position_now["positionAddress"] != position["positionAddress"]:
                    return self._pause("A posição selecionada mudou")
                if hyp_now.decimals != hyp.decimals:
                    return self._pause("A precisão do contrato mudou")
                if not live and (hyp_now.signed_position != initial_signed or hyp_now.open_orders):
                    return self._pause("A posição ou as ordens reais mudaram")
                if live and hyp_now.open_orders:
                    return self._pause("Foram detectadas ordens abertas neste mercado")
                if live and abs(min(hyp_now.signed_position, Decimal("0"))) * hyp_now.mark > AUTO_MAX_POSITION_NOTIONAL:
                    return self._pause("O short real ultrapassou o limite total de US$ 600")
                if hyp_now.mark <= lower or hyp_now.mark >= upper:
                    return self._pause("O preço saiu da faixa da LP")
                if hyp_now.oracle <= 0 or abs(hyp_now.mark - hyp_now.oracle) / hyp_now.oracle > divergence_limit:
                    return self._pause("Mark e oráculo divergiram mais de 0,75%")
                if live and position_now.get("currentPrice") is None:
                    return self._pause("A fonte deixou de fornecer o preço independente da LP")
                lp_price = decimal(position_now.get("currentPrice") or hyp_now.mark, "preço da LP")
                if abs(lp_price - hyp_now.mark) / hyp_now.mark > divergence_limit:
                    return self._pause("Preço da LP e Hyperliquid divergiram mais de 0,75%")

                movement = hyp_now.mark / anchor - Decimal("1")
                if abs(movement) >= step:
                    current_short = abs(min(hyp_now.signed_position, Decimal("0")))
                    difference = target - (current_short if live else virtual_short)
                    size = abs(difference).quantize(quantum, rounding=ROUND_DOWN)
                    notional = size * hyp_now.mark
                    if size > 0 and notional >= AUTO_MIN_ORDER_NOTIONAL:
                        if live:
                            result = self._execute_auto_adjustment(position_now, hyp_now, target)
                            if result:
                                virtual_short = result["currentShort"]
                                anchor = result["anchor"]
                        else:
                            action = "VENDER" if difference > 0 else "COMPRAR"
                            before = virtual_short
                            virtual_short = virtual_short + size if difference > 0 else max(Decimal("0"), virtual_short - size)
                            self._event("adjustment", f"SIMULAR {action} {size} {position['hedgeSymbol']}", size=size, before=before, after=virtual_short, target=target, mark=hyp_now.mark)
                            anchor = hyp_now.mark
                    elif size > 0:
                        self._event("below-minimum", f"Ajuste de US$ {notional:.2f} aguardando próximo nível", size=size, target=target, mark=hyp_now.mark)

                snapshot = {
                    "position": position_now,
                    "market": hyp_now.market,
                    "mark": hyp_now.mark,
                    "oracle": hyp_now.oracle,
                    "lpPrice": lp_price,
                    "basisPercent": abs(lp_price - hyp_now.mark) / hyp_now.mark * 100,
                    "realShort": abs(min(hyp_now.signed_position, Decimal("0"))),
                    "virtualShort": virtual_short,
                    "targetShort": target,
                    "anchor": anchor,
                    "lower": lower,
                    "upper": upper,
                    "stepPercent": 0.5,
                    "live": live,
                    "pendingNotional": abs(target - (abs(min(hyp_now.signed_position, Decimal('0'))) if live else virtual_short)) * hyp_now.mark,
                }
                with self.lock:
                    self.state.update({"snapshot": json_safe(snapshot), "updatedAt": now_iso()})
        except NeutralisError as error:
            self._pause(str(error))
        except Exception:
            self._pause("Falha inesperada; consulte os logs do container")

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {"config": dict(self.config), "monitor": json_safe(dict(self.state)), "events": self.events(50), "dryRun": not bool((self.state.get("snapshot") or {}).get("live")), "ordersEnabled": True, "apiWalletConfigured": API_KEY_FILE.exists(), "autoLimits": {"stepPercent": 0.5, "pollSeconds": AUTO_POLL_SECONDS, "maxExecutionAttempts": AUTO_MAX_EXECUTION_ATTEMPTS, "maxPositionNotional": 600, "minOrderNotional": 10}}


MONITOR = NeutralisMonitor()


class Handler(SimpleHTTPRequestHandler):
    server_version = "NeutralisUmbrel/0.1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(json_safe(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0 or length > 32_768:
            raise NeutralisError("Corpo da requisição inválido")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise NeutralisError("JSON inválido")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                return self.send_json(MONITOR.public_state())
            if path == "/api/positions":
                return self.send_json({"positions": MONITOR.positions(), "updatedAt": now_iso()})
            if path == "/api/events":
                limit = int(parse_qs(urlparse(self.path).query).get("limit", ["100"])[0])
                return self.send_json({"events": MONITOR.events(limit)})
            if path == "/healthz":
                return self.send_json({"ok": True, "dryRun": True})
            return super().do_GET()
        except (NeutralisError, ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            traceback.print_exc()
            return self.send_json(
                {"error": f"Falha interna ao consultar a posição ({type(error).__name__}): {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                return self.send_json({"config": MONITOR.save_config(self.read_json())})
            if path == "/api/trading/key":
                return self.send_json(MONITOR.save_api_key(self.read_json()))
            if path == "/api/monitor/start":
                MONITOR.start()
                return self.send_json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/monitor/start-live":
                incoming = self.read_json()
                MONITOR.start(live=True, confirmation=str(incoming.get("confirmation", "")))
                return self.send_json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/monitor/stop":
                MONITOR.stop()
                return self.send_json({"ok": True})
            return self.send_json({"error": "Endpoint não encontrado"}, HTTPStatus.NOT_FOUND)
        except (NeutralisError, ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            traceback.print_exc()
            return self.send_json(
                {"error": f"Falha interna ao salvar a configuração ({type(error).__name__}): {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def main() -> None:
    prepare_data_permissions()
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Neutralis Umbrel ouvindo na porta {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
