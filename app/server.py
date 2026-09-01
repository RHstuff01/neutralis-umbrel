#!/usr/bin/env python3
"""Neutralis Umbrel: monitor de hedge dinâmico em dry-run ou modo real."""

from __future__ import annotations

import json
import base64
import hashlib
import math
import os
import re
import subprocess
import threading
import traceback
from itertools import count
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
try:
    from eth_utils import keccak
except ImportError:  # permite testes de leitura sem as deps de produção
    def keccak(*_: Any, **__: Any) -> bytes:
        raise NeutralisError("Dependência Ethereum indisponível para consultar Uniswap")


DATA_DIR = Path(os.environ.get("NEUTRALIS_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "events.jsonl"
API_KEY_FILE = DATA_DIR / "hyperliquid-api-wallet.key"
SOLANA_RPC_FILE = DATA_DIR / "solana-rpc.url"
ROBINHOOD_RPC_FILE = DATA_DIR / "robinhood-rpc.url"
TELEGRAM_FILE = DATA_DIR / "telegram-alert.json"
BYREAL_URL = "https://api2.byreal.io/byreal/api/dex/v2/position/list"
BYREAL_MINT_LIST_URL = "https://api2.byreal.io/byreal/api/dex/v2/mint/list"
HYP_INFO_URL = "https://api.hyperliquid.xyz/info"
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
ORCA_DISCOVERY_HELPER = Path(__file__).resolve().parent / "orca-discovery.mjs"
RAYDIUM_MINT_URL = "https://api-v3.raydium.io/mint/ids"
ORCA_TOKEN_URL = "https://api.orca.so/v2/solana/tokens"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
ORCA_IMMUTABLE_WHIRLPOOL_PROGRAM = "iwhrLHdsgrvmnwU8GF2FSmyabSMjfHwFGJAX2ufJ3ZN"
ORCA_WHIRLPOOL_PROGRAMS = (ORCA_WHIRLPOOL_PROGRAM, ORCA_IMMUTABLE_WHIRLPOOL_PROGRAM)
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ORCA_POSITION_DISCRIMINATOR = bytes.fromhex("aabc8fe47a40f7d0")
ORCA_WHIRLPOOL_DISCRIMINATOR = bytes.fromhex("3f95d10ce1806309")
ORCA_POSITION_BUNDLE_DISCRIMINATOR = bytes([129, 169, 175, 65, 185, 95, 32, 100])
DEFAULT_SOLANA_WALLET = "6BYJDhDgA73eGbLQCPvkvwrJLLi5w1yvBeqzCAnJRmfw"
DEFAULT_HYP_ACCOUNT = "0x622dF631Bb769123FC7b8FEd0d2C363045aceDCF"
LIVE_SLIPPAGE = Decimal("0.003")
AUTO_NARROW_STEP = Decimal("0.0025")
AUTO_WIDE_STEP = Decimal("0.005")
AUTO_NARROW_RANGE = Decimal("0.03")
AUTO_EXECUTION_PRICE_DRIFT = Decimal("0.005")
AUTO_MIN_ORDER_NOTIONAL = Decimal("10")
AUTO_POLL_SECONDS = 2
AUTO_RETRY_SECONDS = 1
# Uma IOC que não encontra livro não deve abandonar o hedge. O preço-limite
# vai ficando mais agressivo até este teto e depois continua tentando nele,
# sempre podendo ser interrompido manualmente pelo usuário.
AUTO_RETRY_SLIPPAGES = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.03"))
PRIVATE_KEY_PATTERN = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{20,}$")
TELEGRAM_CHAT_PATTERN = re.compile(r"^-?\d{5,20}$")
SOLANA_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,15}$")
STABLE_SYMBOLS = {"USD", "USDC", "USDT", "USDS", "PYUSD"}
# SPYx é a unidade de ETF próxima de US$ 770. O perp `mkts:US500` usa a
# mesma escala. `xyz:SP500` é um índice próximo de US$ 7.700 e, portanto,
# não pode ser usado como hedge 1:1 da quantidade de SPYx na LP.
SYMBOL_ALIASES = {"AAPLX": "AAPL", "CRCLX": "CRCL", "COINX": "COIN", "SPYX": "US500"}
# `None` representa o mercado perp principal da Hyperliquid.  Nos endpoints
# da API ele não recebe o campo `dex` e o nome do contrato não tem prefixo.
# Os RWAs tokenizados permanecem no DEX xyz; US500 usa mkts por ter a mesma
# escala unitária de SPYx na Orca.
HYP_DEX_BY_SYMBOL: dict[str, str | None] = {"US500": "mkts", "ZEC": None, "SOL": None, "SKR": None}
HYP_DEX_BY_SYMBOL["PENGU"] = None
# O RPC oficial é o preferencial. Ele é público e pode ficar indisponível ou
# limitado; o segundo endpoint permite que a leitura da LP continue no Umbrel
# sem depender de uma única infraestrutura.
ROBINHOOD_RPC_URLS = (
    "https://rpc.mainnet.chain.robinhood.com",
    "https://robinhood-rpc.publicnode.com",
    # O explorador oficial também expõe JSON-RPC de leitura e serve como
    # terceira rota independente quando os nós públicos não resolvem no DNS.
    "https://robinhoodchain.blockscout.com/api/eth-rpc",
)
ROBINHOOD_CHAIN_ID = 4663
UNISWAP_V4_POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
UNISWAP_V4_STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
ROBINHOOD_BLOCKSCOUT_API = "https://robinhoodchain.blockscout.com/api/v2"
KNOWN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1": "CRCLX",
    # SP500 xStock (SPYx). O endpoint de tokens da Orca nem sempre devolve
    # este mint a tempo; sem este mapeamento uma posição SPYx/USDC válida era
    # descartada antes de aparecer na lista de posições.
    "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W": "SPYX",
    # ZEC nativo encapsulado em Solana usado pela pool Orca ZEC/USDC.
    # O catálogo público da Orca pode não responder durante a descoberta e,
    # nesse caso, a posição seria descartada antes de chegar ao hedge ZEC.
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": "ZEC",
    # Seeker (SKR) da pool Orca SKR/USDC.  Na Hyperliquid o contrato
    # correspondente é o perp principal SKR (exibido na interface como
    # SKR-USD), portanto é consultado e negociado sem prefixo de DEX.
    "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3": "SKR",
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
    for path in (CONFIG_FILE, LOG_FILE, SOLANA_RPC_FILE, ROBINHOOD_RPC_FILE, API_KEY_FILE, TELEGRAM_FILE):
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


def solana_rpc_url() -> str:
    try:
        stored = SOLANA_RPC_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    return stored or SOLANA_RPC_URL


def solana_request(payload: dict[str, Any]) -> Any:
    return json_request(solana_rpc_url(), payload)


def robinhood_rpc_urls() -> tuple[str, ...]:
    """Prioriza o RPC dedicado guardado localmente, sem expor a URL/chave."""
    try:
        dedicated = ROBINHOOD_RPC_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        dedicated = ""
    urls = ((dedicated,) if dedicated else ()) + ROBINHOOD_RPC_URLS
    return tuple(dict.fromkeys(urls))


def evm_word(value: int | str) -> str:
    if isinstance(value, int):
        return f"{value:064x}"
    text = str(value).lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{1,64}", text):
        raise NeutralisError("Parâmetro EVM inválido")
    return text.rjust(64, "0")


def evm_selector(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


def robinhood_request(method: str, params: list[Any]) -> Any:
    last_error: NeutralisError | None = None
    for rpc_url in robinhood_rpc_urls():
        try:
            root = json_request(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if not isinstance(root, dict) or root.get("error") or "result" not in root:
                raise NeutralisError("Resposta inválida do RPC da Robinhood Chain")
            return root["result"]
        except NeutralisError as error:
            last_error = error
    raise NeutralisError("Falha de rede nos RPCs da Robinhood Chain") from last_error


def robinhood_call(contract: str, signature: str, *arguments: int | str) -> str:
    if not EVM_PATTERN.fullmatch(contract):
        raise NeutralisError("Contrato Robinhood inválido")
    data = "0x" + evm_selector(signature) + "".join(evm_word(argument) for argument in arguments)
    result = robinhood_request("eth_call", [{"to": contract, "data": data}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise NeutralisError("Resposta inválida da Robinhood Chain")
    return result[2:]


def abi_int24(word: int) -> int:
    result = word & 0xFFFFFF
    return result - (1 << 24) if result & (1 << 23) else result


def abi_string(data: str) -> str:
    raw = bytes.fromhex(data)
    if len(raw) == 32:  # alguns ERC-20 antigos retornam bytes32
        return raw.rstrip(b"\0").decode("utf-8", "replace")
    if len(raw) < 64:
        return ""
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode("utf-8", "replace")


def erc20_metadata(address: str) -> tuple[str, int]:
    symbol = abi_string(robinhood_call(address, "symbol()"))
    decimals = int(robinhood_call(address, "decimals()") or "0", 16)
    if not symbol or not 0 <= decimals <= 36:
        raise NeutralisError("Token ERC-20 inválido na Robinhood Chain")
    return symbol.upper(), decimals


def uniswap_v4_owner_tokens(wallet: str) -> list[int]:
    if not EVM_PATTERN.fullmatch(wallet):
        raise NeutralisError("Carteira EVM inválida")
    # Não usamos eth_getLogs desde o bloco zero: o plano gratuito da Alchemy
    # limita essa consulta a dez blocos. O Blockscout indexa os NFTs da
    # carteira e fornece justamente os token IDs da PositionManager.
    root = json_request(
        f"{ROBINHOOD_BLOCKSCOUT_API}/addresses/{wallet}/nft/collections?type=ERC-721,ERC-1155"
    )
    collections = root.get("items", []) if isinstance(root, dict) else []
    if not isinstance(collections, list):
        raise NeutralisError("Resposta inválida ao localizar NFTs Uniswap")
    tokens: list[int] = []
    for collection in collections:
        token = collection.get("token", {}) if isinstance(collection, dict) else {}
        address = str(token.get("address") or token.get("address_hash") or "") if isinstance(token, dict) else ""
        if address.lower() != UNISWAP_V4_POSITION_MANAGER:
            continue
        instances = collection.get("token_instances", [])
        for instance in instances if isinstance(instances, list) else []:
            token_id = instance.get("id", instance.get("token_id")) if isinstance(instance, dict) else None
            try:
                tokens.append(int(str(token_id)))
            except (TypeError, ValueError):
                continue
    return sorted(set(tokens))


def uniswap_v4_position(token_id: int, pool_id: str) -> dict[str, Any] | None:
    pool_id = pool_id.lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{64}", pool_id):
        raise NeutralisError("Pool ID Uniswap V4 inválido")
    encoded_position = robinhood_call(UNISWAP_V4_POSITION_MANAGER, "getPoolAndPositionInfo(uint256)", token_id)
    if len(encoded_position) < 384:
        return None
    words = [int(encoded_position[i : i + 64], 16) for i in range(0, 384, 64)]
    if len(words) != 6:
        return None
    token0, token1 = f"0x{words[0] & ((1 << 160) - 1):040x}", f"0x{words[1] & ((1 << 160) - 1):040x}"
    packed = words[5]
    tick_lower, tick_upper = abi_int24(packed >> 8), abi_int24(packed >> 32)
    if not tick_lower < tick_upper:
        return None
    pool_key_encoded = "".join(evm_word(item) for item in (token0, token1, words[2], words[3] & ((1 << 256) - 1), f"0x{words[4] & ((1 << 160) - 1):040x}"))
    actual_pool_id = keccak(hexstr="0x" + pool_key_encoded).hex()
    if actual_pool_id.lower() != pool_id:
        return None
    symbol0, decimals0 = erc20_metadata(token0)
    symbol1, decimals1 = erc20_metadata(token1)
    if {symbol0, symbol1} != {"PENGU", "USDG"}:
        return None
    slot = robinhood_call(UNISWAP_V4_STATE_VIEW, "getSlot0(bytes32)", "0x" + pool_id)
    sqrt_price_x96 = int(slot[:64], 16)
    if sqrt_price_x96 <= 0:
        return None
    liquidity = int(robinhood_call(UNISWAP_V4_POSITION_MANAGER, "getPositionLiquidity(uint256)", token_id)[:64], 16)
    if liquidity <= 0:
        return None
    raw_price_1_per_0 = Decimal(sqrt_price_x96) ** 2 / Decimal(2) ** 192
    price_1_per_0 = raw_price_1_per_0 * Decimal(10) ** (decimals0 - decimals1)
    pengu_is_0 = symbol0 == "PENGU"
    current_price = price_1_per_0 if pengu_is_0 else Decimal(1) / price_1_per_0
    tick_price_1_per_0 = lambda tick: Decimal("1.0001") ** tick * Decimal(10) ** (decimals0 - decimals1)
    lower_raw, upper_raw = tick_price_1_per_0(tick_lower), tick_price_1_per_0(tick_upper)
    lower_price, upper_price = (lower_raw, upper_raw) if pengu_is_0 else (Decimal(1) / upper_raw, Decimal(1) / lower_raw)
    # Converte a liquidez raw V4 para a quantidade efetiva de PENGU da LP.
    sqrt_l = Decimal("1.0001") ** (Decimal(tick_lower) / 2) * Decimal(2) ** 96
    sqrt_u = Decimal("1.0001") ** (Decimal(tick_upper) / 2) * Decimal(2) ** 96
    sqrt_p = Decimal(sqrt_price_x96)
    if sqrt_p <= sqrt_l:
        amount0_raw, amount1_raw = Decimal(liquidity) * (sqrt_u - sqrt_l) * Decimal(2) ** 96 / (sqrt_l * sqrt_u), Decimal(0)
    elif sqrt_p >= sqrt_u:
        amount0_raw, amount1_raw = Decimal(0), Decimal(liquidity) * (sqrt_u - sqrt_l) / Decimal(2) ** 96
    else:
        amount0_raw = Decimal(liquidity) * (sqrt_u - sqrt_p) * Decimal(2) ** 96 / (sqrt_p * sqrt_u)
        amount1_raw = Decimal(liquidity) * (sqrt_p - sqrt_l) / Decimal(2) ** 96
    amount0, amount1 = amount0_raw / Decimal(10) ** decimals0, amount1_raw / Decimal(10) ** decimals1
    asset_amount = amount0 if pengu_is_0 else amount1
    quote_amount = amount1 if pengu_is_0 else amount0
    liquidity_usd = asset_amount * current_price + quote_amount
    # A conversão inversa mantém a posição monitorável quando ela está acima
    # da faixa (100% USDG). Nesse caso o alvo de PENGU é naturalmente zero.
    normalized_liquidity = (
        asset_amount / base_target(Decimal(1), current_price, lower_price, upper_price)
        if asset_amount
        else lp_liquidity(liquidity_usd, current_price, lower_price, upper_price)
    )
    return {"source": "uniswap", "positionAddress": str(token_id), "personalPositionAddress": str(token_id), "poolAddress": "0x" + pool_id, "pair": "PENGU / USDG", "assetSymbol": "PENGU", "hedgeSymbol": "PENGU", "hedgeMode": "units", "quoteSymbol": "USDG", "liquidityUsd": liquidity_usd, "normalizedLiquidity": normalized_liquidity, "lowerPrice": lower_price, "upperPrice": upper_price, "currentPrice": current_price, "importable": bool(liquidity_usd > 0)}


def uniswap_v4_positions(wallet: str, pool_id: str) -> list[dict[str, Any]]:
    return [position for token_id in uniswap_v4_owner_tokens(wallet) if (position := uniswap_v4_position(token_id, pool_id)) is not None]


def hyp_symbol(lp_symbol: str) -> str:
    symbol = str(lp_symbol or "").strip().upper()
    symbol = SYMBOL_ALIASES.get(symbol, symbol)
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise NeutralisError("Símbolo incompatível com a Hyperliquid")
    return symbol


def hedge_mode(lp_symbol: str) -> str:
    return "units"


def hyp_dex(symbol: str) -> str | None:
    """DEX Hyperliquid que contém o contrato correspondente."""
    return HYP_DEX_BY_SYMBOL.get(hyp_symbol(symbol), "xyz")


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


def program_pda(seeds: list[bytes], program_id: str) -> str:
    program = base58_decode(program_id)
    if len(program) != 32 or any(len(seed) > 32 for seed in seeds):
        raise NeutralisError("Seeds inválidas para derivar a posição")
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_ed25519_point(digest):
            return base58_encode(digest)
    raise NeutralisError("Não foi possível derivar a posição")


def position_pda(nft_mint: str, program_id: str) -> str:
    mint = base58_decode(nft_mint)
    if len(mint) != 32:
        raise NeutralisError("NFT da posição inválido")
    return program_pda([b"position", mint], program_id)


def raydium_position_pda(nft_mint: str) -> str:
    return position_pda(nft_mint, RAYDIUM_CLMM_PROGRAM)


def orca_position_pda(nft_mint: str, program_id: str = ORCA_WHIRLPOOL_PROGRAM) -> str:
    return position_pda(nft_mint, program_id)


def orca_position_bundle_pda(nft_mint: str, program_id: str = ORCA_WHIRLPOOL_PROGRAM) -> str:
    mint = base58_decode(nft_mint)
    if len(mint) != 32:
        raise NeutralisError("NFT do bundle Orca inválido")
    return program_pda([b"position_bundle", mint], program_id)


def orca_bundled_position_pda(bundle_address: str, bundle_index: int, program_id: str = ORCA_WHIRLPOOL_PROGRAM) -> str:
    bundle = base58_decode(bundle_address)
    if len(bundle) != 32 or not 0 <= bundle_index < 256:
        raise NeutralisError("Bundle Orca inválido")
    return program_pda([b"bundled_position", bundle, str(bundle_index).encode("ascii")], program_id)


def solana_account(address: str, expected_owner: str | None = None) -> bytes:
    response = solana_request({
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
        response = solana_request({
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
    except (NeutralisError, StopIteration):
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
        "hedgeMode": hedge_mode(asset_symbol),
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
            response = solana_request({
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [wallet, {"programId": program_id}, {"encoding": "base64", "commitment": "confirmed"}],
            })
        except NeutralisError:
            continue
        successful_queries += 1
        rows = response.get("result", {}).get("value") if isinstance(response, dict) else None
        for row in rows if isinstance(rows, list) else []:
            encoded = row.get("account", {}).get("data") if isinstance(row, dict) else None
            if not isinstance(encoded, list) or not encoded:
                continue
            try:
                token_account = base64.b64decode(encoded[0], validate=True)
            except Exception:
                continue
            # O layout-base é idêntico nos programas SPL Token e Token-2022:
            # mint [0:32], owner [32:64] e amount u64 [64:72]. Extensões vêm
            # depois desse cabeçalho. É também o método usado pelo SDK da Orca.
            if len(token_account) < 72 or int.from_bytes(token_account[64:72], "little") != 1:
                continue
            mint = base58_encode(token_account[:32])
            if SOLANA_PATTERN.fullmatch(mint):
                mints.add(mint)
    # Helius e outros RPCs compatíveis com DAS enxergam NFTs de posição que
    # nem sempre aparecem na enumeração SPL padrão (por exemplo posições
    # agrupadas/Token-2022). É uma contingência: RPCs comuns apenas ignoram
    # esse método e a descoberta tradicional continua funcionando.
    try:
        assets = solana_request({
            "jsonrpc": "2.0", "id": 2, "method": "getAssetsByOwner",
            "params": {"ownerAddress": wallet, "page": 1, "limit": 1000,
                       "displayOptions": {"showFungible": False}},
        })
        rows = assets.get("result", {}).get("items", []) if isinstance(assets, dict) else []
        for row in rows if isinstance(rows, list) else []:
            mint = str(row.get("id", "")) if isinstance(row, dict) else ""
            token_info = row.get("token_info", {}) if isinstance(row, dict) else {}
            decimals = token_info.get("decimals") if isinstance(token_info, dict) else None
            if SOLANA_PATTERN.fullmatch(mint) and (decimals in {None, 0}):
                mints.add(mint)
    except (NeutralisError, StopIteration):
        pass
    if not successful_queries and not mints:
        raise NeutralisError("Falha ao consultar os NFTs da carteira Solana")
    return sorted(mints)


def orca_position(
    nft_mint: str,
    position_data: bytes | None = None,
    program_id: str = ORCA_WHIRLPOOL_PROGRAM,
) -> dict[str, Any]:
    if not SOLANA_PATTERN.fullmatch(nft_mint):
        raise NeutralisError("NFT da posição Orca inválido")
    position_address = orca_position_pda(nft_mint, program_id)
    data = position_data if position_data is not None else solana_account(position_address, program_id)
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

    pool_data = solana_account(pool_address, program_id)
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
        "programId": program_id,
        "positionAddress": nft_mint,
        "personalPositionAddress": position_address,
        "poolAddress": pool_address,
        "pair": f"{asset_symbol} / {quote_symbol}",
        "assetSymbol": asset_symbol,
        "hedgeSymbol": hyp_symbol(asset_symbol),
        "hedgeMode": hedge_mode(asset_symbol),
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
        for program_id in ORCA_WHIRLPOOL_PROGRAMS:
            if orca_position_pda(nft_mint, program_id) == address:
                result = orca_position(nft_mint, account_data, program_id)
                result["personalPositionAddress"] = address
                return result
        raise NeutralisError("A conta Orca pertence a um tipo de posição não reconhecido")
    if len(account_data) >= 8 and account_data[:8] == ORCA_WHIRLPOOL_DISCRIMINATOR:
        return None
    last_error = None
    for program_id in ORCA_WHIRLPOOL_PROGRAMS:
        try:
            return orca_position(address, program_id=program_id)
        except NeutralisError as error:
            last_error = error
    raise last_error or NeutralisError("Posição Orca não encontrada")


def orca_sdk_positions(wallet: str) -> list[dict[str, Any]]:
    environment = {**os.environ, "SOLANA_RPC_URL": solana_rpc_url()}
    try:
        completed = subprocess.run(
            ["node", str(ORCA_DISCOVERY_HELPER), wallet],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NeutralisError("SDK oficial da Orca não está disponível") from error
    if completed.returncode != 0:
        raise NeutralisError("SDK oficial da Orca não conseguiu consultar a carteira")
    try:
        payload = json.loads(completed.stdout)
        rows = payload.get("positions", [])
    except (json.JSONDecodeError, AttributeError) as error:
        raise NeutralisError("Resposta inválida do SDK oficial da Orca") from error
    if not isinstance(rows, list):
        raise NeutralisError("Resposta inválida do SDK oficial da Orca")

    positions = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("positionAddress", ""))
        mint = str(row.get("positionMint", ""))
        program_id = str(row.get("programId", ""))
        if not SOLANA_PATTERN.fullmatch(address) or not SOLANA_PATTERN.fullmatch(mint):
            continue
        if program_id not in ORCA_WHIRLPOOL_PROGRAMS:
            continue
        try:
            data = solana_account(address, program_id)
            position = orca_position(mint, data, program_id)
            position["personalPositionAddress"] = address
            if row.get("positionBundleAddress"):
                position.update({
                    "positionAddress": address,
                    "positionNftMint": mint,
                    "positionBundleAddress": str(row["positionBundleAddress"]),
                })
            positions.append(position)
        except NeutralisError:
            continue
    return positions


def orca_positions_manual(wallet: str) -> list[dict[str, Any]]:
    nft_mints = solana_nft_mints(wallet)
    positions = []
    for program_id in ORCA_WHIRLPOOL_PROGRAMS:
        derived = {orca_position_pda(mint, program_id): mint for mint in nft_mints}
        bundle_derived = {orca_position_bundle_pda(mint, program_id): mint for mint in nft_mints}
        accounts = solana_accounts(list(derived) + list(bundle_derived), program_id)
        for address, mint in derived.items():
            data = accounts.get(address)
            if data is None or len(data) < 96 or data[:8] != ORCA_POSITION_DISCRIMINATOR:
                continue
            try:
                positions.append(orca_position(mint, data, program_id))
            except NeutralisError:
                continue
        bundled: dict[str, tuple[str, str, int]] = {}
        for bundle_address, mint in bundle_derived.items():
            data = accounts.get(bundle_address)
            if data is None or len(data) < 72 or data[:8] != ORCA_POSITION_BUNDLE_DISCRIMINATOR:
                continue
            if public_key_at(data, 8) != mint:
                continue
            for index in range(256):
                if data[40 + index // 8] & (1 << (index % 8)):
                    position_address = orca_bundled_position_pda(bundle_address, index, program_id)
                    bundled[position_address] = (mint, bundle_address, index)
        bundled_accounts = solana_accounts(list(bundled), program_id) if bundled else {}
        for address, data in bundled_accounts.items():
            mint, bundle_address, index = bundled[address]
            if len(data) < 96 or data[:8] != ORCA_POSITION_DISCRIMINATOR:
                continue
            try:
                position = orca_position(mint, data, program_id)
                position.update({
                    "positionAddress": address,
                    "positionNftMint": mint,
                    "personalPositionAddress": address,
                    "positionBundleAddress": bundle_address,
                    "bundleIndex": index,
                })
                positions.append(position)
            except NeutralisError:
                continue
    return positions


def orca_positions(wallet: str) -> list[dict[str, Any]]:
    """Prioriza o SDK oficial; mantém a leitura nativa como contingência."""
    sdk_error = None
    try:
        positions = orca_sdk_positions(wallet)
        if positions:
            return positions
    except NeutralisError as error:
        sdk_error = str(error)
    try:
        positions = orca_positions_manual(wallet)
        if positions:
            return positions
    except NeutralisError as error:
        fallback_error = str(error)
        detail = f" SDK: {sdk_error}." if sdk_error else " SDK: nenhuma posição decodificável."
        raise NeutralisError(f"Nenhuma posição Orca foi encontrada nesta carteira.{detail} Leitor alternativo: {fallback_error}.") from error
    detail = f" SDK: {sdk_error}." if sdk_error else " SDK oficial: 0 posições decodificáveis."
    raise NeutralisError(
        "Nenhuma posição Orca foi encontrada nesta carteira."
        f"{detail} Leitor alternativo: 0 posições decodificáveis. "
        "Verifique se a LP é realmente um Whirlpool da Orca e se a carteira pública é a proprietária do NFT da posição."
    )


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
        "hedgeMode": hedge_mode(asset["symbol"]),
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
    dex: str | None = "xyz"


def hyp_state(account: str, symbol: str) -> HypState:
    if not EVM_PATTERN.fullmatch(account):
        raise NeutralisError("Conta Hyperliquid inválida")
    symbol = hyp_symbol(symbol)
    dex = hyp_dex(symbol)
    market = f"{dex}:{symbol}" if dex else symbol

    # O perp principal (ZEC, SOL etc.) é consultado sem `dex`. Mandar
    # `dex: "xyz"` faz a Hyperliquid procurar um contrato inexistente.
    def info_payload(request_type: str, **values: str) -> dict[str, str]:
        payload = {"type": request_type, **values}
        if dex:
            payload["dex"] = dex
        return payload

    metadata, contexts = json_request(HYP_INFO_URL, info_payload("metaAndAssetCtxs"))
    clearinghouse = json_request(HYP_INFO_URL, info_payload("clearinghouseState", user=account))
    orders = json_request(HYP_INFO_URL, info_payload("frontendOpenOrders", user=account))
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
        dex=dex,
    )




def lp_liquidity(value: Decimal, price: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    if not (value > 0 and Decimal("0") < lower < upper and price > 0):
        raise NeutralisError("Preço inválido para calcular a liquidez da LP")
    sqrt_lower, sqrt_price, sqrt_upper = lower.sqrt(), price.sqrt(), upper.sqrt()
    base_per_liquidity = (sqrt_upper - sqrt_price) / (sqrt_price * sqrt_upper)
    stable_per_liquidity = sqrt_price - sqrt_lower
    # Fora da faixa a posição continua existindo: abaixo dela fica toda no
    # ativo-base; acima, toda na cotação estável. A liquidez precisa continuar
    # calculável para que o hedge seja mantido, e não pausado.
    if price <= lower:
        base_per_liquidity = (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
        stable_per_liquidity = Decimal("0")
    elif price >= upper:
        base_per_liquidity = Decimal("0")
        stable_per_liquidity = sqrt_upper - sqrt_lower
    return value / (base_per_liquidity * price + stable_per_liquidity)


def base_target(liquidity: Decimal, price: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    sqrt_lower, sqrt_upper = lower.sqrt(), upper.sqrt()
    if price <= lower:
        return liquidity * (Decimal("1") / sqrt_lower - Decimal("1") / sqrt_upper)
    if price >= upper:
        return Decimal("0")
    sqrt_price = price.sqrt()
    return liquidity * (Decimal("1") / sqrt_price - Decimal("1") / sqrt_upper)


def adaptive_rebalance_step(lower: Decimal, upper: Decimal) -> Decimal:
    """Escolhe o gatilho pela largura total da faixa da LP.

    A largura é medida sobre o ponto médio: faixas abaixo de 3% usam 0,25%;
    faixas de 3% ou mais usam 0,50%.
    """
    if not (Decimal("0") < lower < upper):
        raise NeutralisError("Faixa inválida para definir o gatilho")
    midpoint = (lower + upper) / Decimal("2")
    return AUTO_NARROW_STEP if (upper - lower) / midpoint < AUTO_NARROW_RANGE else AUTO_WIDE_STEP


def target_at_reference_price(
    position: dict[str, Any],
    liquidity: Decimal,
    price: Decimal,
    lower: Decimal,
    upper: Decimal,
    hyp_mark: Decimal,
) -> Decimal:
    """Calcula a exposição da LP para um preço de referência do ativo.

    Para a LP SPYx, o preço de referência acompanha o US500 da Hyperliquid
    desde a âncora. Assim o gatilho e o ajuste não ficam à espera de um swap
    na Orca apenas para atualizar o tick on-chain.
    """
    target = base_target(liquidity, price, lower, upper)
    if position.get("hedgeMode") == "notional":
        target = target * price / hyp_mark
    return target


def hedge_basis(position: dict[str, Any], lp_price: Decimal, hyp_mark: Decimal, reference_ratio: Decimal | None = None) -> Decimal:
    if hyp_mark <= 0 or lp_price <= 0:
        raise NeutralisError("Preço inválido para calcular o basis do hedge")
    ratio = lp_price / hyp_mark
    # O spread inicial entre dois mercados é normal. A proteção mede a
    # alteração desse spread a partir da âncora, não o spread absoluto.
    if reference_ratio is None or reference_ratio <= 0:
        return abs(ratio - Decimal("1"))
    return abs(ratio / reference_ratio - Decimal("1"))


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


class NeutralisMonitor:
    def __init__(self, slot: str = "1") -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.slot = slot
        # O slot 1 preserva os dados já usados pelo app. O segundo slot tem
        # arquivos próprios: configuração e registro jamais se misturam.
        self.config_file = CONFIG_FILE if slot == "1" else DATA_DIR / f"config-{slot}.json"
        self.log_file = LOG_FILE if slot == "1" else DATA_DIR / f"events-{slot}.jsonl"
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.execution_lock = threading.Lock()
        self.manual_stop_requested = False
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
            "evmWallet": DEFAULT_HYP_ACCOUNT,
            "uniswapTokenId": "",
            "hyperliquidAccount": DEFAULT_HYP_ACCOUNT,
            "positionAddress": "",
            "maxPositionNotional": "600",
            "stepPercent": "0.5",
        }
        try:
            stored = json.loads(self.config_file.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update({key: str(stored.get(key, defaults[key])) for key in defaults})
        except (OSError, json.JSONDecodeError):
            pass
        return defaults

    def save_config(self, incoming: dict[str, Any]) -> dict[str, str]:
        source = str(incoming.get("source", self.config["source"])).lower()
        wallet = str(incoming.get("solanaWallet", self.config["solanaWallet"]))
        evm_wallet = str(incoming.get("evmWallet", self.config["evmWallet"]))
        uniswap_token_id = str(incoming.get("uniswapTokenId", self.config.get("uniswapTokenId", ""))).strip()
        account = str(incoming.get("hyperliquidAccount", self.config["hyperliquidAccount"]))
        position = str(incoming.get("positionAddress", self.config["positionAddress"]))
        max_notional = decimal(incoming.get("maxPositionNotional", self.config["maxPositionNotional"]), "limite máximo do short")
        step_percent = decimal(incoming.get("stepPercent", self.config["stepPercent"]), "gatilho de ajuste")
        if source not in {"byreal", "raydium", "orca", "uniswap"}:
            raise NeutralisError("Fonte de liquidez inválida")
        if source != "uniswap" and not SOLANA_PATTERN.fullmatch(wallet):
            raise NeutralisError("Carteira Solana inválida")
        if source == "uniswap" and not EVM_PATTERN.fullmatch(evm_wallet):
            raise NeutralisError("Carteira EVM inválida")
        if not EVM_PATTERN.fullmatch(account):
            raise NeutralisError("Conta Hyperliquid inválida")
        if source == "uniswap" and position and not re.fullmatch(r"0x[0-9a-fA-F]{64}", position):
            raise NeutralisError("Pool ID Uniswap V4 inválido")
        if source == "uniswap" and uniswap_token_id and not re.fullmatch(r"[1-9][0-9]{0,77}", uniswap_token_id):
            raise NeutralisError("NFT Uniswap inválido; use somente o número do Token ID")
        if source != "uniswap" and position and not SOLANA_PATTERN.fullmatch(position):
            raise NeutralisError("Endereço da posição inválido")
        if not Decimal("10") <= max_notional <= Decimal("100000"):
            raise NeutralisError("O limite máximo do short deve ficar entre US$ 10 e US$ 100.000")
        if not Decimal("0.05") <= step_percent <= Decimal("5"):
            raise NeutralisError("O gatilho de ajuste deve ficar entre 0,05% e 5,00%")
        with self.lock:
            if self.state["mode"] == "running":
                raise NeutralisError("Pare o monitor antes de alterar a configuração")
            self.config = {"source": source, "solanaWallet": wallet, "evmWallet": evm_wallet, "uniswapTokenId": uniswap_token_id, "hyperliquidAccount": account, "positionAddress": position, "maxPositionNotional": str(max_notional), "stepPercent": str(step_percent)}
            self.config_file.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
            os.chmod(self.config_file, 0o600)
        return dict(self.config)

    def max_position_notional(self) -> Decimal:
        return decimal(self.config.get("maxPositionNotional", "600"), "limite máximo do short")

    def rebalance_step(self, lower: Decimal, upper: Decimal) -> Decimal:
        # O gatilho é definido pelo usuário para cada pool. Mantemos os
        # parâmetros da faixa para tornar explícito que esta escolha pertence
        # à posição selecionada, e não ao mercado inteiro.
        del lower, upper
        return decimal(self.config.get("stepPercent", "0.5"), "gatilho de ajuste") / Decimal("100")

    def save_api_key(self, incoming: dict[str, Any]) -> dict[str, Any]:
        key = str(incoming.get("privateKey", "")).strip()
        if not PRIVATE_KEY_PATTERN.fullmatch(key):
            raise NeutralisError("Chave privada da API Wallet inválida")
        normalized = key if key.startswith("0x") else f"0x{key}"
        API_KEY_FILE.write_text(normalized, encoding="ascii")
        os.chmod(API_KEY_FILE, 0o600)
        return {"configured": True}

    def save_telegram_alert(self, incoming: dict[str, Any]) -> dict[str, Any]:
        token = str(incoming.get("botToken", "")).strip()
        chat_id = str(incoming.get("chatId", "")).strip()
        if not TELEGRAM_TOKEN_PATTERN.fullmatch(token):
            raise NeutralisError("Token do bot Telegram inválido")
        if not TELEGRAM_CHAT_PATTERN.fullmatch(chat_id):
            raise NeutralisError("Chat ID do Telegram inválido")
        TELEGRAM_FILE.write_text(json.dumps({"botToken": token, "chatId": chat_id}), encoding="utf-8")
        os.chmod(TELEGRAM_FILE, 0o600)
        return {"configured": True}

    @staticmethod
    def telegram_configured() -> bool:
        try:
            stored = json.loads(TELEGRAM_FILE.read_text(encoding="utf-8"))
            return bool(isinstance(stored, dict) and stored.get("botToken") and stored.get("chatId"))
        except (OSError, json.JSONDecodeError):
            return False

    def _notify_telegram(self, message: str) -> None:
        try:
            stored = json.loads(TELEGRAM_FILE.read_text(encoding="utf-8"))
            token, chat_id = str(stored["botToken"]), str(stored["chatId"])
            text = f"⚠️ Neutralis: Pool {self.slot}.\n{message}"
            json_request(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": text})
        except Exception:
            # Uma falha no Telegram jamais pode parar, esconder ou reiniciar
            # o monitor; o motivo original continua disponível no registro.
            return

    def save_solana_rpc(self, incoming: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(incoming.get("endpoint", "")).strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise NeutralisError("Endpoint RPC Solana inválido; use uma URL HTTPS")
        if len(endpoint) > 2048:
            raise NeutralisError("Endpoint RPC Solana muito longo")
        SOLANA_RPC_FILE.write_text(endpoint, encoding="utf-8")
        os.chmod(SOLANA_RPC_FILE, 0o600)
        return {"configured": True, "host": parsed.hostname}

    def save_robinhood_rpc(self, incoming: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(incoming.get("endpoint", "")).strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise NeutralisError("Endpoint RPC Robinhood inválido; use uma URL HTTPS")
        if len(endpoint) > 2048:
            raise NeutralisError("Endpoint RPC Robinhood muito longo")
        ROBINHOOD_RPC_FILE.write_text(endpoint, encoding="utf-8")
        os.chmod(ROBINHOOD_RPC_FILE, 0o600)
        return {"configured": True, "host": parsed.hostname}

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
            # A SDK só carrega o perp principal quando a lista inclui "".
            # Ao informar apenas DEXs HIP-3, ela não cria o mapa interno para
            # ZEC/SOL e `order("ZEC", ...)` termina em KeyError.
            perp_dexs=["", "xyz", "mkts"],
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

        for attempt in count():
            if attempt:
                position, hyp, lower, upper, _, _ = self._live_snapshot()
                if original_position and position.get("positionAddress") != original_position:
                    raise NeutralisError("A posição selecionada mudou durante o ajuste")
                if position.get("currentPrice") is None:
                    raise NeutralisError("A fonte deixou de fornecer o preço independente da LP")

            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            if hyp.signed_position > 0:
                raise NeutralisError("A conta ficou long; monitor pausado")
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
                if resulting_notional > self.max_position_notional():
                    raise NeutralisError(f"Short-alvo ultrapassaria o limite total de US$ {self.max_position_notional():.2f}")
            notional = size * hyp.mark
            if notional < AUTO_MIN_ORDER_NOTIONAL:
                return None

            slippage = AUTO_RETRY_SLIPPAGES[min(attempt, len(AUTO_RETRY_SLIPPAGES) - 1)]
            response = None
            try:
                with self.execution_lock:
                    current = hyp_state(self.config["hyperliquidAccount"], position["hedgeSymbol"])
                    if current.open_orders or current.signed_position != hyp.signed_position:
                        raise NeutralisError("A posição ou as ordens mudaram durante a validação")
                    # O mark pode mudar rápido entre a consulta e o envio.
                    # Usamos o mark recém-lido para tornar a IOC executável,
                    # em vez de pausar com o hedge incompleto.
                    hyp = current
                    if not is_buy and (current_short + size) * hyp.mark > self.max_position_notional():
                        raise NeutralisError(f"Short-alvo ultrapassaria o limite total de US$ {self.max_position_notional():.2f}")
                    limit_price = hyp.mark * (Decimal("1") + slippage if is_buy else Decimal("1") - slippage)
                    limit_price = Decimal(f"{limit_price:.5g}")
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
                # A Hyp devolve mensagens diferentes para a mesma situação:
                # IOC sem contraparte. Ambas devem seguir para o próximo
                # limite mais agressivo, e não pausar o hedge de imediato.
                error_text = str(error).upper()
                retryable = (
                    "IOC" in error_text
                    or "IOCCANCEL" in error_text
                    or "COULD NOT IMMEDIATELY MATCH" in error_text
                    or "RESTING ORDERS" in error_text
                )
                if not retryable:
                    raise
                attempt_number = attempt + 1
                max_attempt = len(AUTO_RETRY_SLIPPAGES)
                # Registra o escalonamento e, no patamar máximo, uma vez a
                # cada dez tentativas para manter o histórico legível.
                if attempt_number <= max_attempt or attempt_number % 10 == 0:
                    suffix = (
                        f"tentando novamente com slippage máximo de {slippage * 100:.2f}%"
                        if attempt_number >= max_attempt
                        else f"nova tentativa {attempt_number + 1}/{max_attempt}"
                    )
                    self._event(
                        "live-retry",
                        f"IOC não executada; {suffix}",
                        target=target,
                        mark=hyp.mark,
                        response=response,
                        attempt=attempt_number,
                        slippagePercent=slippage * 100,
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
            if self.stop_event.wait(AUTO_RETRY_SECONDS):
                raise NeutralisError("Monitor interrompido durante o ajuste")

    def positions(self) -> list[dict[str, Any]]:
        if self.config["source"] == "uniswap":
            pool_id = self.config["positionAddress"]
            if not pool_id:
                return []
            token_id = self.config.get("uniswapTokenId", "")
            if token_id:
                position = uniswap_v4_position(int(token_id), pool_id)
                if position is None:
                    raise NeutralisError(
                        "NFT Uniswap não corresponde a esta pool, está sem liquidez ou não pôde ser lido pelo RPC. "
                        "Confirme o Token ID numérico do NFT PENGU/USDG."
                    )
                return [position]
            positions = uniswap_v4_positions(self.config["evmWallet"], pool_id)
            if not positions:
                raise NeutralisError(
                    "Nenhum NFT Uniswap foi localizado automaticamente. Informe o Token ID numérico do NFT no campo próprio "
                    "para não depender do Blockscout."
                )
            return positions
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
            # Quando já há uma posição selecionada por NFT/PDA, ela pode ser
            # lida diretamente. Não deixe uma falha temporária na enumeração
            # de todos os NFTs da carteira interromper o monitor dessa LP.
            try:
                discovered = orca_positions(self.config["solanaWallet"])
            except NeutralisError:
                if direct:
                    return [direct]
                raise
            if direct and all(item["positionAddress"] != direct["positionAddress"] for item in discovered):
                discovered.insert(0, direct)
            if selected and not discovered and direct_error:
                raise direct_error
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
        lp_price = decimal(position.get("currentPrice"), "preço da LP")
        if position.get("normalizedLiquidity") is not None:
            liquidity = decimal(position["normalizedLiquidity"], "normalizedLiquidity")
        else:
            liquidity = lp_liquidity(value, lp_price, lower, upper)
        target = target_at_reference_price(position, liquidity, lp_price, lower, upper, hyp.mark)
        return position, hyp, lower, upper, liquidity, target

    def _event(self, event: str, message: str, **details: Any) -> None:
        record = {"at": now_iso(), "event": event, "message": message, **json_safe(details)}
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self.lock:
            self.state["lastEvent"] = record

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.log_file.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)) :]
            return [json.loads(line) for line in reversed(lines) if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    def start(self, live: bool = False, confirmation: str = "") -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise NeutralisError("O monitor já está em execução")
            if live:
                position, hyp, _, _, _, _ = self._live_snapshot()
                expected = "ATIVAR"
                if confirmation.strip().upper() != expected:
                    raise NeutralisError("Confirmação incorreta. Digite exatamente: ATIVAR")
                if not API_KEY_FILE.exists():
                    raise NeutralisError("Cadastre a chave da API Wallet antes de ativar o modo real")
                if position["hedgeSymbol"] != hyp_symbol(position["assetSymbol"]):
                    raise NeutralisError("Mapeamento do contrato não pôde ser validado")
            self.manual_stop_requested = False
            self.stop_event.clear()
            self.state.update({"mode": "starting", "message": "Validando fontes de dados", "updatedAt": now_iso()})
            self.thread = threading.Thread(target=self._run, args=(live,), name="neutralis-live" if live else "neutralis-dry-run", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.manual_stop_requested = True
            self.state.update({"mode": "stopped", "message": "Monitor interrompido pelo usuário", "updatedAt": now_iso()})
        self._event("stop", "Monitor interrompido pelo usuário")

    def _pause(self, message: str) -> None:
        with self.lock:
            if self.manual_stop_requested:
                return
            self.state.update({"mode": "paused", "message": message, "updatedAt": now_iso()})
        self._event("pause", message)
        self._notify_telegram(f"Pausou automaticamente.\nMotivo: {message}")

    def _finish_upper_exit(self, market: str, mark: Decimal, live: bool) -> None:
        message = "Faixa superior atingida; short zerado e monitor encerrado" if live else "Faixa superior atingida; zeramento simulado e monitor encerrado"
        self.stop_event.set()
        with self.lock:
            self.state.update({"mode": "stopped", "message": message, "updatedAt": now_iso()})
        self._event("upper-exit", message, market=market, mark=mark, live=live)

    def _run(self, live: bool = False) -> None:
        try:
            position, hyp, lower, upper, liquidity, target = self._live_snapshot()
            step = self.rebalance_step(lower, upper)
            if hyp.signed_position > 0:
                raise NeutralisError("A conta está long; o monitor exige posição zero ou short")
            if live and abs(hyp.signed_position) * hyp.mark > self.max_position_notional():
                raise NeutralisError(f"O short real já ultrapassa o limite total de US$ {self.max_position_notional():.2f}")
            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            if live and position.get("currentPrice") is None:
                raise NeutralisError("A fonte não forneceu preço independente da LP; modo real bloqueado")
            lp_price = decimal(position.get("currentPrice") or hyp.mark, "preço da LP")
            # A Hyp é o mercado contínuo que determina quando o hedge deve
            # reagir. A Orca permanece como fonte da faixa e da liquidez.
            lp_anchor = lp_price
            hyp_anchor = hyp.mark
            ratio_anchor = lp_anchor / hyp_anchor
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
                lp_anchor = lp_price
                hyp_anchor = hyp.mark
                ratio_anchor = lp_anchor / hyp_anchor
            initial_signed = hyp.signed_position
            virtual_short = abs(min(initial_signed, Decimal("0")))
            quantum = Decimal(1).scaleb(-hyp.decimals)
            anchor = lp_anchor
            initial_snapshot = {
                "position": position,
                "market": hyp.market,
                "mark": hyp.mark,
                "oracle": hyp.oracle,
                "lpPrice": lp_price,
                "basisPercent": hedge_basis(position, lp_price, hyp.mark) * 100,
                "basisFromAnchorPercent": Decimal("0"),
                "movementFromAnchorPercent": Decimal("0"),
                "hedgeRatio": lp_price / hyp.mark,
                "hypAnchor": hyp_anchor,
                "projectedLpPrice": lp_price,
                "hedgeMode": position.get("hedgeMode", "units"),
                "realShort": abs(min(hyp.signed_position, Decimal("0"))),
                "virtualShort": virtual_short,
                "targetShort": target,
                "anchor": anchor,
                "lower": lower,
                "upper": upper,
                "stepPercent": step * 100,
                "live": live,
                "pendingNotional": abs(target - virtual_short) * hyp.mark,
            }
            with self.lock:
                label = "MODO REAL ativo" if live else "Dry-run ativo"
                self.state.update({"mode": "running", "message": f"{label} · aguardando nível de {step * 100:.2f}%", "snapshot": json_safe(initial_snapshot), "updatedAt": now_iso()})
            start_message = (
                f"MODO REAL iniciado em {hyp.market}; delta inicial corrigido"
                if live and initial_adjusted
                else f"{'MODO REAL' if live else 'Dry-run'} iniciado em {hyp.market}; delta inicial dentro do mínimo negociável"
                if live
                else f"Dry-run iniciado em {hyp.market}; nenhuma ordem inicial"
            )
            self._event("start-live" if live else "start", start_message, mark=hyp.mark, lpPrice=lp_price, lower=lower, upper=upper, anchor=anchor, hedgeRatio=ratio_anchor)
            awaiting_upper_reentry = False
            below_range = lp_price <= lower

            while not self.stop_event.wait(AUTO_POLL_SECONDS):
                position_now, hyp_now, lower, upper, liquidity, target = self._live_snapshot()
                step = self.rebalance_step(lower, upper)
                if position_now["positionAddress"] != position["positionAddress"]:
                    return self._pause("A posição selecionada mudou")
                if hyp_now.decimals != hyp.decimals:
                    return self._pause("A precisão do contrato mudou")
                if not live and (hyp_now.signed_position != initial_signed or hyp_now.open_orders):
                    return self._pause("A posição ou as ordens reais mudaram")
                if live and hyp_now.open_orders:
                    return self._pause("Foram detectadas ordens abertas neste mercado")
                if live and abs(min(hyp_now.signed_position, Decimal("0"))) * hyp_now.mark > self.max_position_notional():
                    return self._pause(f"O short real ultrapassou o limite total de US$ {self.max_position_notional():.2f}")
                if live and position_now.get("currentPrice") is None:
                    return self._pause("A fonte deixou de fornecer o preço independente da LP")
                lp_price = decimal(position_now.get("currentPrice") or hyp_now.mark, "preço da LP")
                basis_from_anchor = hedge_basis(position_now, lp_price, hyp_now.mark, ratio_anchor)

                # A projeção usa a relação LP/Hyp registrada na última
                # âncora. É ela que alimenta a fórmula CLMM entre swaps na
                # Orca; o gatilho é exclusivamente o mark da Hyperliquid.
                movement = hyp_now.mark / hyp_anchor - Decimal("1")
                projected_lp_price = lp_anchor * hyp_now.mark / hyp_anchor
                target = target_at_reference_price(position_now, liquidity, projected_lp_price, lower, upper, hyp_now.mark)
                # A saída inferior deixa a LP 100% no ativo. O hedge segue
                # normalmente, mas o aviso é útil para o usuário reavaliar a
                # faixa. Só avisamos na transição para não gerar spam a cada
                # consulta de dois segundos.
                if projected_lp_price <= lower:
                    if not below_range:
                        below_range = True
                        message = "Pool saiu pela faixa inferior; o hedge continua ativo."
                        self._event("lower-exit", message, market=hyp_now.market, mark=hyp_now.mark, live=live)
                        self._notify_telegram(message)
                elif below_range:
                    below_range = False
                    self._event("lower-reentry", "Preço reentrou acima da faixa inferior; hedge continua ativo", market=hyp_now.market, mark=hyp_now.mark, live=live)
                # Acima da faixa a LP fica 100% em USDC. Fecha o short uma
                # única vez, mas mantém o processo vivo: se o preço voltar
                # para dentro da faixa, o hedge é reconstruído automaticamente.
                if projected_lp_price >= upper:
                    target = Decimal("0")
                    if not awaiting_upper_reentry and live:
                        current_short = abs(min(hyp_now.signed_position, Decimal("0")))
                        if current_short:
                            result = self._execute_auto_adjustment(position_now, hyp_now, Decimal("0"))
                            if not result or result["currentShort"] > 0:
                                return self._pause("Faixa superior atingida, mas não foi possível zerar todo o short")
                            virtual_short = result["currentShort"]
                    elif not awaiting_upper_reentry:
                        virtual_short = Decimal("0")
                    if not awaiting_upper_reentry:
                        awaiting_upper_reentry = True
                        self._event(
                            "upper-exit",
                            "Faixa superior atingida; short zerado e aguardando reentrada automática",
                            market=hyp_now.market,
                            mark=hyp_now.mark,
                            live=live,
                        )
                        self._notify_telegram("Pool saiu pela faixa superior; short zerado e aguardando reentrada automática.")
                    # A nova âncora evita reexecutar o mesmo zeramento em
                    # todos os ciclos enquanto a LP permanece 100% em USDC.
                    lp_anchor, hyp_anchor, anchor = projected_lp_price, hyp_now.mark, projected_lp_price
                    ratio_anchor, movement, basis_from_anchor = lp_price / hyp_anchor, Decimal("0"), Decimal("0")
                    projected_lp_price = lp_anchor
                elif awaiting_upper_reentry:
                    awaiting_upper_reentry = False
                    self._event(
                        "upper-reentry",
                        "Preço reentrou na faixa; recalculando e restaurando hedge automaticamente",
                        market=hyp_now.market,
                        mark=hyp_now.mark,
                        target=target,
                        live=live,
                    )
                    if live:
                        result = self._execute_auto_adjustment(position_now, hyp_now, target)
                        if result:
                            virtual_short = result["currentShort"]
                    else:
                        virtual_short = target
                    lp_anchor, hyp_anchor, anchor = projected_lp_price, hyp_now.mark, projected_lp_price
                    ratio_anchor, movement, basis_from_anchor = lp_price / hyp_anchor, Decimal("0"), Decimal("0")
                    projected_lp_price = lp_anchor
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
                                # Conserva o preço projetado no próximo
                                # degrau, mesmo que o tick Orca ainda esteja
                                # temporariamente atrasado em relação à Hyp.
                                lp_anchor = projected_lp_price
                                hyp_anchor = hyp_now.mark
                                ratio_anchor = lp_price / hyp_anchor
                                anchor = lp_anchor
                                movement = Decimal("0")
                                projected_lp_price = lp_anchor
                                basis_from_anchor = Decimal("0")
                        else:
                            action = "VENDER" if difference > 0 else "COMPRAR"
                            before = virtual_short
                            virtual_short = virtual_short + size if difference > 0 else max(Decimal("0"), virtual_short - size)
                            self._event("adjustment", f"SIMULAR {action} {size} {position['hedgeSymbol']}", size=size, before=before, after=virtual_short, target=target, mark=hyp_now.mark)
                            lp_anchor = projected_lp_price
                            hyp_anchor = hyp_now.mark
                            ratio_anchor = lp_price / hyp_anchor
                            anchor = lp_anchor
                            movement = Decimal("0")
                            projected_lp_price = lp_anchor
                            basis_from_anchor = Decimal("0")
                    elif size > 0:
                        self._event("below-minimum", f"Ajuste de US$ {notional:.2f} aguardando próximo nível", size=size, target=target, mark=hyp_now.mark)

                snapshot = {
                    "position": position_now,
                    "market": hyp_now.market,
                    "mark": hyp_now.mark,
                    "oracle": hyp_now.oracle,
                    "lpPrice": lp_price,
                    "basisPercent": hedge_basis(position_now, lp_price, hyp_now.mark) * 100,
                    "basisFromAnchorPercent": basis_from_anchor * 100,
                    "movementFromAnchorPercent": movement * 100,
                    "hedgeRatio": lp_price / hyp_now.mark,
                    "hypAnchor": hyp_anchor,
                    "projectedLpPrice": projected_lp_price,
                    "hedgeMode": position_now.get("hedgeMode", "units"),
                    "realShort": abs(min(hyp_now.signed_position, Decimal("0"))),
                    "virtualShort": virtual_short,
                    "targetShort": target,
                    "anchor": anchor,
                    "lower": lower,
                    "upper": upper,
                    "stepPercent": step * 100,
                    "live": live,
                    "pendingNotional": abs(target - (abs(min(hyp_now.signed_position, Decimal('0'))) if live else virtual_short)) * hyp_now.mark,
                }
                with self.lock:
                    self.state.update({"snapshot": json_safe(snapshot), "updatedAt": now_iso()})
        except NeutralisError as error:
            self._pause(str(error))
        except Exception as error:
            # O Umbrel não expõe os logs do container na interface normal.  Sem
            # este detalhe, uma falha da SDK, de assinatura ou da resposta da
            # corretora vira apenas uma mensagem genérica impossível de agir.
            # Nunca incluímos request/headers/chave: apenas o tipo e a mensagem
            # curta da exceção que o Python já devolveu.
            detail = re.sub(r"0x[a-fA-F0-9]{64}", "[chave ocultada]", str(error)).strip()
            detail = detail[:240] or "sem detalhes retornados"
            self._pause(f"Falha técnica ao executar ajuste ({type(error).__name__}): {detail}")

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {"config": dict(self.config), "monitor": json_safe(dict(self.state)), "events": self.events(50), "dryRun": not bool((self.state.get("snapshot") or {}).get("live")), "ordersEnabled": True, "apiWalletConfigured": API_KEY_FILE.exists(), "telegramConfigured": self.telegram_configured(), "solanaRpcConfigured": SOLANA_RPC_FILE.exists(), "solanaRpcHost": urlparse(solana_rpc_url()).hostname, "robinhoodRpcConfigured": ROBINHOOD_RPC_FILE.exists(), "autoLimits": {"pollSeconds": AUTO_POLL_SECONDS, "maxSlippagePercent": float(AUTO_RETRY_SLIPPAGES[-1] * 100), "maxPositionNotional": float(self.max_position_notional()), "minOrderNotional": 10}}


MONITORS = {"1": NeutralisMonitor("1"), "2": NeutralisMonitor("2")}
# Compatibilidade com testes e chamadas internas antigas: slot 1.
MONITOR = MONITORS["1"]


def monitor_for_slot(value: Any) -> NeutralisMonitor:
    slot = str(value or "1")
    if slot not in MONITORS:
        raise NeutralisError("Monitor inválido")
    return MONITORS[slot]


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
                # Retorna os dois estados de uma vez; assim a interface pode
                # mostrar e atualizar as duas pools simultaneamente.
                selected = monitor_for_slot(parse_qs(urlparse(self.path).query).get("slot", ["1"])[0])
                selected_state = selected.public_state()
                return self.send_json({
                    # Campos do slot selecionado preservam a interface/API
                    # anterior e permitem alternar entre as duas pools.
                    **selected_state,
                    "monitors": {slot: monitor.public_state() for slot, monitor in MONITORS.items()},
                    "apiWalletConfigured": API_KEY_FILE.exists(),
                    "telegramConfigured": MONITOR.telegram_configured(),
                    "solanaRpcConfigured": SOLANA_RPC_FILE.exists(),
                    "robinhoodRpcConfigured": ROBINHOOD_RPC_FILE.exists(),
                })
            if path == "/api/positions":
                monitor = monitor_for_slot(parse_qs(urlparse(self.path).query).get("slot", ["1"])[0])
                return self.send_json({"positions": monitor.positions(), "updatedAt": now_iso()})
            if path == "/api/events":
                limit = int(parse_qs(urlparse(self.path).query).get("limit", ["100"])[0])
                monitor = monitor_for_slot(parse_qs(urlparse(self.path).query).get("slot", ["1"])[0])
                return self.send_json({"events": monitor.events(limit)})
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
                incoming = self.read_json()
                return self.send_json({"config": monitor_for_slot(incoming.get("slot")).save_config(incoming)})
            if path == "/api/trading/key":
                return self.send_json(MONITOR.save_api_key(self.read_json()))
            if path == "/api/alerts/telegram":
                return self.send_json(MONITOR.save_telegram_alert(self.read_json()))
            if path == "/api/solana/rpc":
                return self.send_json(MONITOR.save_solana_rpc(self.read_json()))
            if path == "/api/robinhood/rpc":
                return self.send_json(MONITOR.save_robinhood_rpc(self.read_json()))
            if path == "/api/monitor/start":
                monitor_for_slot(self.read_json().get("slot")).start()
                return self.send_json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/monitor/start-live":
                incoming = self.read_json()
                monitor_for_slot(incoming.get("slot")).start(live=True, confirmation=str(incoming.get("confirmation", "")))
                return self.send_json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/monitor/stop":
                monitor_for_slot(self.read_json().get("slot")).stop()
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
