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
from decimal import Decimal, ROUND_DOWN, ROUND_UP
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
ESTIMATED_TAKER_FEE_RATE = Decimal("0.00045")
AUTO_POLL_SECONDS = 2
AUTO_RETRY_SECONDS = 1
# O short-base é encerrado pouco abaixo do próprio preço médio. A margem busca
# cobrir taxa/spread e impede que a recompra programada transforme a recuperação
# em perda deliberada do short.
BASE_RECOVERY_EXIT_BUFFER = Decimal("0.001")
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
SYMBOL_ALIASES = {
    "AAPLX": "AAPL", "AMZNX": "AMZN", "CRCLX": "CRCL", "COINX": "COIN", "SPYX": "US500",
    "NVDAX": "NVDA", "NVIDIA": "NVDA",
    "SPACEX": "SPCX", "SPACEXX": "SPCX", "SPCXX": "SPCX",
    "GOOGLX": "GOOGL", "GOOGLE": "GOOGL", "ALPHABET": "GOOGL",
    "WNEAR": "NEAR", "WNEARX": "NEAR",
}
# `None` representa o mercado perp principal da Hyperliquid.  Nos endpoints
# da API ele não recebe o campo `dex` e o nome do contrato não tem prefixo.
# Os RWAs tokenizados permanecem no DEX xyz; US500 usa mkts por ter a mesma
# escala unitária de SPYx na Orca.
HYP_DEX_BY_SYMBOL: dict[str, str | None] = {
    "US500": "mkts", "ZEC": None, "SOL": None, "SKR": None, "NEAR": None, "AVAX": None,
}
HYP_DEX_BY_SYMBOL["PENGU"] = None
# Alguns emissores exibem o ativo com sufixo USD na interface, mas a API
# pode publicar o mesmo perp sem sufixo. O monitor consulta o catálogo e usa
# o nome que estiver efetivamente ativo para não enviar ordens inválidas.
HYP_MARKET_ALTERNATIVES = {
    "IBM": ("IBM",), "AMZN": ("AMZN",), "NVDA": ("NVDA",), "SPCX": ("SPCX",), "GOOGL": ("GOOGL",),
}
# Ativos já homologados para as LPs Uniswap V3/V4 da Robinhood Chain. Os demais
# pares não são importados até terem um perp correspondente confirmado.
ROBINHOOD_UNISWAP_ASSETS = {"PENGU", "IBM", "NVDA", "SPCX", "GOOGL"}
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
# Implantação canônica V3 na Robinhood Chain (chain ID 4663).
UNISWAP_V3_POSITION_MANAGER = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
ROBINHOOD_BLOCKSCOUT_API = "https://robinhoodchain.blockscout.com/api/v2"
# Arc Mainnet (chain ID 5042). A implantação V4 é canônica e usa a mesma
# interface da Robinhood, mas a cotação desta LP é USDC.
ARC_RPC_URLS = ("https://rpc.mainnet.arc.io", "https://rpc.arc-scan.org")
ARC_CHAIN_ID = 5042
ARC_UNISWAP_V4_POSITION_MANAGER = "0x6049c9a0e26405c0985f9e3685c87d0ae917f82b"
ARC_UNISWAP_V4_STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
ARC_UNISWAP_ASSETS = {"CRCL"}
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
    # AVAX nativo em Solana usado pelo Whirlpool AVAX/USDC
    # HFCMZM2NiLG74Fsx15VL3WFn5pR2ZxDNuXe1STntE8oh. O hedge correspondente
    # é o perp principal AVAX da Hyperliquid.
    "avaxGHCq3T7hoxd73oY2KY9hJSTaeMibXvHy5KNzh5D": "AVAX",
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
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            # Alguns RPCs públicos (incluindo Arc) bloqueiam o User-Agent
            # padrão do urllib mesmo para chamadas JSON-RPC válidas.
            "user-agent": "Neutralis-Hedge/0.8",
        },
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


def evm_request(rpc_urls: tuple[str, ...], chain_name: str, method: str, params: list[Any]) -> Any:
    last_error: NeutralisError | None = None
    for rpc_url in rpc_urls:
        try:
            root = json_request(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if not isinstance(root, dict) or root.get("error") or "result" not in root:
                raise NeutralisError(f"Resposta inválida do RPC da {chain_name}")
            return root["result"]
        except NeutralisError as error:
            last_error = error
    raise NeutralisError(f"Falha de rede nos RPCs da {chain_name}") from last_error


def evm_call(rpc_urls: tuple[str, ...], chain_name: str, contract: str, signature: str, *arguments: int | str) -> str:
    if not EVM_PATTERN.fullmatch(contract):
        raise NeutralisError(f"Contrato {chain_name} inválido")
    data = "0x" + evm_selector(signature) + "".join(evm_word(argument) for argument in arguments)
    result = evm_request(rpc_urls, chain_name, "eth_call", [{"to": contract, "data": data}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise NeutralisError(f"Resposta inválida da {chain_name}")
    return result[2:]


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


def evm_erc20_metadata(call: Any, address: str, chain_name: str) -> tuple[str, int]:
    symbol = abi_string(call(address, "symbol()"))
    decimals = int(call(address, "decimals()") or "0", 16)
    if not symbol or not 0 <= decimals <= 36:
        raise NeutralisError(f"Token ERC-20 inválido na {chain_name}")
    return symbol.upper(), decimals


def uniswap_owner_tokens(wallet: str, position_manager: str) -> list[int]:
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
        if address.lower() != position_manager.lower():
            continue
        instances = collection.get("token_instances", [])
        for instance in instances if isinstance(instances, list) else []:
            token_id = instance.get("id", instance.get("token_id")) if isinstance(instance, dict) else None
            try:
                tokens.append(int(str(token_id)))
            except (TypeError, ValueError):
                continue
    return sorted(set(tokens))


def uniswap_v4_owner_tokens(wallet: str) -> list[int]:
    return uniswap_owner_tokens(wallet, UNISWAP_V4_POSITION_MANAGER)


def uniswap_v3_owner_tokens(wallet: str) -> list[int]:
    return uniswap_owner_tokens(wallet, UNISWAP_V3_POSITION_MANAGER)


def concentrated_position_result(
    source: str,
    token_id: int,
    pool_address: str,
    symbol0: str,
    decimals0: int,
    symbol1: str,
    decimals1: int,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
    quote_symbols: set[str] | None = None,
    allowed_assets: set[str] | None = None,
) -> dict[str, Any] | None:
    """Normaliza uma posição concentrada EVM V3/V4 para o cálculo do hedge."""
    if sqrt_price_x96 <= 0 or liquidity <= 0 or not tick_lower < tick_upper:
        return None
    quotes = quote_symbols or {"USDG"}
    quote_symbol = next((symbol for symbol in (symbol0, symbol1) if symbol in quotes), None)
    if quote_symbol is None:
        return None
    asset_symbol = symbol1 if symbol0 == quote_symbol else symbol0
    if hyp_symbol(asset_symbol) not in (allowed_assets or ROBINHOOD_UNISWAP_ASSETS):
        return None
    raw_price_1_per_0 = Decimal(sqrt_price_x96) ** 2 / Decimal(2) ** 192
    price_1_per_0 = raw_price_1_per_0 * Decimal(10) ** (decimals0 - decimals1)
    asset_is_0 = symbol0 == asset_symbol
    current_price = price_1_per_0 if asset_is_0 else Decimal(1) / price_1_per_0
    tick_price_1_per_0 = lambda tick: Decimal("1.0001") ** tick * Decimal(10) ** (decimals0 - decimals1)
    lower_raw, upper_raw = tick_price_1_per_0(tick_lower), tick_price_1_per_0(tick_upper)
    lower_price, upper_price = (lower_raw, upper_raw) if asset_is_0 else (Decimal(1) / upper_raw, Decimal(1) / lower_raw)
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
    asset_amount = amount0 if asset_is_0 else amount1
    quote_amount = amount1 if asset_is_0 else amount0
    liquidity_usd = asset_amount * current_price + quote_amount
    normalized_liquidity = (
        asset_amount / base_target(Decimal(1), current_price, lower_price, upper_price)
        if asset_amount
        else lp_liquidity(liquidity_usd, current_price, lower_price, upper_price)
    )
    return {
        "source": source,
        "positionAddress": str(token_id),
        "personalPositionAddress": str(token_id),
        "poolAddress": pool_address,
        "pair": f"{asset_symbol} / {quote_symbol}",
        "assetSymbol": asset_symbol,
        "hedgeSymbol": hyp_symbol(asset_symbol),
        "hedgeMode": "units",
        "quoteSymbol": quote_symbol,
        "liquidityUsd": liquidity_usd,
        "normalizedLiquidity": normalized_liquidity,
        "lowerPrice": lower_price,
        "upperPrice": upper_price,
        "currentPrice": current_price,
        "assetAmount": asset_amount,
        "quoteAmount": quote_amount,
        "importable": bool(liquidity_usd > 0),
    }


def uniswap_v3_position(token_id: int, pool_address: str) -> dict[str, Any] | None:
    if not EVM_PATTERN.fullmatch(pool_address):
        raise NeutralisError("Endereço da pool Uniswap V3 inválido")
    encoded = robinhood_call(UNISWAP_V3_POSITION_MANAGER, "positions(uint256)", token_id)
    if len(encoded) < 64 * 12:
        return None
    words = [int(encoded[i : i + 64], 16) for i in range(0, 64 * 12, 64)]
    token0 = f"0x{words[2] & ((1 << 160) - 1):040x}"
    token1 = f"0x{words[3] & ((1 << 160) - 1):040x}"
    fee, tick_lower, tick_upper, liquidity = words[4], abi_int24(words[5]), abi_int24(words[6]), words[7]
    pool_token0 = "0x" + robinhood_call(pool_address, "token0()")[-40:]
    pool_token1 = "0x" + robinhood_call(pool_address, "token1()")[-40:]
    pool_fee = int(robinhood_call(pool_address, "fee()")[-64:], 16)
    if (token0.lower(), token1.lower(), fee) != (pool_token0.lower(), pool_token1.lower(), pool_fee):
        return None
    symbol0, decimals0 = erc20_metadata(token0)
    symbol1, decimals1 = erc20_metadata(token1)
    slot = robinhood_call(pool_address, "slot0()")
    return concentrated_position_result(
        "uniswap", token_id, pool_address, symbol0, decimals0, symbol1, decimals1,
        int(slot[:64], 16), tick_lower, tick_upper, liquidity,
    )


def uniswap_v3_positions(wallet: str, pool_address: str) -> list[dict[str, Any]]:
    return [position for token_id in uniswap_v3_owner_tokens(wallet) if (position := uniswap_v3_position(token_id, pool_address)) is not None]


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
    if "USDG" not in {symbol0, symbol1}:
        return None
    asset_symbol = symbol1 if symbol0 == "USDG" else symbol0
    # A permissão usa o símbolo do hedge, mas a posição mantém o símbolo
    # original do token (NVDAx/SPCXx/SpaceXx) para exibição e quantidades.
    if hyp_symbol(asset_symbol) not in ROBINHOOD_UNISWAP_ASSETS:
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
    asset_is_0 = symbol0 == asset_symbol
    current_price = price_1_per_0 if asset_is_0 else Decimal(1) / price_1_per_0
    tick_price_1_per_0 = lambda tick: Decimal("1.0001") ** tick * Decimal(10) ** (decimals0 - decimals1)
    lower_raw, upper_raw = tick_price_1_per_0(tick_lower), tick_price_1_per_0(tick_upper)
    lower_price, upper_price = (lower_raw, upper_raw) if asset_is_0 else (Decimal(1) / upper_raw, Decimal(1) / lower_raw)
    # Converte a liquidez raw V4 para a quantidade efetiva do ativo da LP.
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
    asset_amount = amount0 if asset_is_0 else amount1
    quote_amount = amount1 if asset_is_0 else amount0
    liquidity_usd = asset_amount * current_price + quote_amount
    # A conversão inversa mantém a posição monitorável quando ela está acima
    # da faixa (100% USDG). Nesse caso o alvo do ativo é naturalmente zero.
    normalized_liquidity = (
        asset_amount / base_target(Decimal(1), current_price, lower_price, upper_price)
        if asset_amount
        else lp_liquidity(liquidity_usd, current_price, lower_price, upper_price)
    )
    return {"source": "uniswap", "positionAddress": str(token_id), "personalPositionAddress": str(token_id), "poolAddress": "0x" + pool_id, "pair": f"{asset_symbol} / USDG", "assetSymbol": asset_symbol, "hedgeSymbol": hyp_symbol(asset_symbol), "hedgeMode": "units", "quoteSymbol": "USDG", "liquidityUsd": liquidity_usd, "normalizedLiquidity": normalized_liquidity, "lowerPrice": lower_price, "upperPrice": upper_price, "currentPrice": current_price, "importable": bool(liquidity_usd > 0)}


def uniswap_v4_positions(wallet: str, pool_id: str) -> list[dict[str, Any]]:
    return [position for token_id in uniswap_v4_owner_tokens(wallet) if (position := uniswap_v4_position(token_id, pool_id)) is not None]


def arc_call(contract: str, signature: str, *arguments: int | str) -> str:
    return evm_call(ARC_RPC_URLS, "Arc", contract, signature, *arguments)


def arc_uniswap_v4_position(token_id: int, expected_pool_id: str = "") -> dict[str, Any] | None:
    """Lê uma posição Uniswap V4 na Arc diretamente pelo Token ID."""
    expected = expected_pool_id.lower().removeprefix("0x")
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise NeutralisError("Pool ID Uniswap V4 da Arc inválido")
    encoded = arc_call(ARC_UNISWAP_V4_POSITION_MANAGER, "getPoolAndPositionInfo(uint256)", token_id)
    if len(encoded) < 384:
        return None
    words = [int(encoded[i : i + 64], 16) for i in range(0, 384, 64)]
    token0 = f"0x{words[0] & ((1 << 160) - 1):040x}"
    token1 = f"0x{words[1] & ((1 << 160) - 1):040x}"
    packed = words[5]
    tick_lower, tick_upper = abi_int24(packed >> 8), abi_int24(packed >> 32)
    pool_key_encoded = "".join(evm_word(item) for item in (
        token0, token1, words[2], words[3] & ((1 << 256) - 1),
        f"0x{words[4] & ((1 << 160) - 1):040x}",
    ))
    pool_id = keccak(hexstr="0x" + pool_key_encoded).hex()
    if expected and pool_id.lower() != expected:
        return None
    symbol0, decimals0 = evm_erc20_metadata(arc_call, token0, "Arc")
    symbol1, decimals1 = evm_erc20_metadata(arc_call, token1, "Arc")
    slot = arc_call(ARC_UNISWAP_V4_STATE_VIEW, "getSlot0(bytes32)", "0x" + pool_id)
    liquidity = int(arc_call(
        ARC_UNISWAP_V4_POSITION_MANAGER, "getPositionLiquidity(uint256)", token_id
    )[:64], 16)
    return concentrated_position_result(
        "uniswap", token_id, "0x" + pool_id,
        symbol0, decimals0, symbol1, decimals1,
        int(slot[:64], 16), tick_lower, tick_upper, liquidity,
        quote_symbols={"USDC"}, allowed_assets=ARC_UNISWAP_ASSETS,
    )


def arc_uniswap_owner(token_id: int) -> str:
    encoded = arc_call(ARC_UNISWAP_V4_POSITION_MANAGER, "ownerOf(uint256)", token_id)
    return "0x" + encoded[-40:]


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
    entry_price: Decimal = Decimal("0")


def hyp_state(account: str, symbol: str) -> HypState:
    if not EVM_PATTERN.fullmatch(account):
        raise NeutralisError("Conta Hyperliquid inválida")
    symbol = hyp_symbol(symbol)
    preferred_dex = hyp_dex(symbol)
    candidates = HYP_MARKET_ALTERNATIVES.get(
        symbol,
        (symbol, f"{symbol}USD") if preferred_dex == "xyz" and not symbol.endswith("USD") else (symbol,),
    )

    def info_payload(request_type: str, dex: str | None, **values: str) -> dict[str, str]:
        payload = {"type": request_type, **values}
        if dex:
            payload["dex"] = dex
        return payload

    def market_in_dex(dex: str | None) -> tuple[dict[str, Any], list[Any], int | None]:
        metadata, contexts = json_request(HYP_INFO_URL, info_payload("metaAndAssetCtxs", dex))
        universe = metadata.get("universe", []) if isinstance(metadata, dict) else []
        # Em mercados HIP-3 a própria API pode devolver o nome qualificado
        # (por exemplo ``xyz:AAPL``), mesmo quando o catálogo foi consultado
        # com ``dex=xyz``. Comparamos tanto o nome completo quanto a parte
        # posterior aos dois-pontos.
        index = next(
            (
                i
                for i, row in enumerate(universe)
                if str(row.get("name", "")).upper() in candidates
                or str(row.get("name", "")).upper().rsplit(":", 1)[-1] in candidates
            ),
            None,
        )
        return metadata, contexts, index

    metadata, contexts, index = market_in_dex(preferred_dex)
    dex = preferred_dex
    if index is None:
        dex_rows = json_request(HYP_INFO_URL, {"type": "perpDexs"})
        names = [str(row.get("name", "")) for row in dex_rows if isinstance(row, dict) and row.get("name")]
        for candidate_dex in names:
            if candidate_dex == preferred_dex:
                continue
            # Um DEX recém-publicado ou temporariamente indisponível não pode
            # impedir a procura nos demais catálogos. Só a ausência do ativo
            # em todos eles deve resultar em "contrato não encontrado".
            try:
                metadata, contexts, index = market_in_dex(candidate_dex)
            except NeutralisError:
                continue
            if index is not None:
                dex = candidate_dex
                break
    if index is None:
        requested = f"{preferred_dex}:{symbol}" if preferred_dex else symbol
        raise NeutralisError(f"Contrato {requested} não encontrado na Hyperliquid")
    universe = metadata.get("universe", [])
    active_symbol = str(universe[index].get("name", symbol))
    # Não duplique o DEX quando a API já devolveu ``xyz:AAPL``.
    if ":" in active_symbol:
        catalog_dex, catalog_symbol = active_symbol.rsplit(":", 1)
        market = f"{catalog_dex.lower()}:{catalog_symbol.upper()}"
    else:
        market = active_symbol.upper() if not dex else f"{dex}:{active_symbol.upper()}"
    clearinghouse = json_request(HYP_INFO_URL, info_payload("clearinghouseState", dex, user=account))
    orders = json_request(HYP_INFO_URL, info_payload("frontendOpenOrders", dex, user=account))
    context = contexts[index]
    signed = Decimal("0")
    entry_price = Decimal("0")
    for row in clearinghouse.get("assetPositions", []):
        position = row.get("position", {})
        if str(position.get("coin", "")).upper() in set(candidates) | {market.upper()}:
            signed = decimal(position.get("szi", 0), "Hyperliquid szi")
            entry_price = decimal(position.get("entryPx") or 0, "Hyperliquid entryPx")
            break
    open_orders = sum(1 for row in orders if str(row.get("coin", "")).upper() in set(candidates) | {market.upper()})
    return HypState(
        market=market,
        decimals=int(universe[index]["szDecimals"]),
        mark=decimal(context.get("markPx"), "Hyperliquid markPx"),
        oracle=decimal(context.get("oraclePx", context.get("markPx")), "Hyperliquid oraclePx"),
        signed_position=signed,
        open_orders=open_orders,
        dex=dex,
        entry_price=entry_price,
    )


def hyp_ioc_limit_price(mark: Decimal, slippage: Decimal, is_buy: bool, size_decimals: int) -> Decimal:
    """Preço IOC válido para as regras de precisão da Hyperliquid.

    A Hyp aceita no máximo cinco algarismos significativos e, para perps,
    no máximo ``6 - szDecimals`` casas decimais no preço. O arredondamento
    precisa ser agressivo: para compra arredonda para cima, para venda para
    baixo, evitando transformar uma IOC em ordem inválida ou não executável.
    """
    if mark <= 0 or slippage < 0 or not 0 <= size_decimals <= 6:
        raise NeutralisError("Parâmetros inválidos para preço IOC")
    raw = mark * (Decimal("1") + slippage if is_buy else Decimal("1") - slippage)
    significant_places = 4 - raw.adjusted()
    decimal_places = max(0, min(6 - size_decimals, significant_places))
    tick = Decimal(1).scaleb(-decimal_places)
    return raw.quantize(tick, rounding=ROUND_UP if is_buy else ROUND_DOWN)




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


def principal_metrics(position: dict[str, Any], initial_principal_raw: str) -> dict[str, Decimal | None]:
    """Calcula o resultado do principal da LP sem incluir taxas ou recompensas."""
    current_raw = position.get("liquidityUsd")
    if current_raw is None:
        return {"principalCurrentUsd": None, "principalInitialUsd": None, "principalPnlUsd": None, "principalPnlPercent": None}
    current = decimal(current_raw, "saldo atual da LP")
    if not initial_principal_raw:
        return {"principalCurrentUsd": current, "principalInitialUsd": None, "principalPnlUsd": None, "principalPnlPercent": None}
    initial = decimal(initial_principal_raw, "saldo inicial da LP")
    pnl = current - initial
    return {
        "principalCurrentUsd": current,
        "principalInitialUsd": initial,
        "principalPnlUsd": pnl,
        "principalPnlPercent": pnl / initial * Decimal("100"),
    }


def upside_hedge_signal(
    regime: str,
    mark: Decimal,
    operation_reference: Decimal,
    step: Decimal,
    short_entry_price: Decimal | None = None,
    base_recovery_armed: bool = False,
) -> str | None:
    """Indica a troca de regime da estratégia de participação na alta.

    Uma proteção existente é encerrada pouco abaixo do preço médio do short,
    depois da confirmação temporal. A reentrada posterior é controlada pela
    reversão desde a máxima da recuperação, fora desta função.

    Uma operação realmente nova usa metade do gatilho apenas para descobrir a
    direção inicial. A queda abre a proteção; a alta confirma o regime de
    participação sem enviar ordem.
    """
    if mark <= 0 or operation_reference <= 0 or step <= 0:
        return None
    if regime == "protected" and base_recovery_armed and short_entry_price and short_entry_price > 0:
        close_floor = short_entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
        if mark >= close_floor:
            return "close"
    if regime == "upside" and mark <= operation_reference:
        return "wait"
    if regime in {"initial_wait", "direction_wait"}:
        half_step = step / Decimal("2")
        if mark <= operation_reference * (Decimal("1") - half_step):
            return "open"
        if mark >= operation_reference * (Decimal("1") + half_step):
            return "confirm_upside"
    return None


def open_hedge_lots(lots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retorna somente parcelas ainda abertas, preservando a ordem de criação."""
    return [lot for lot in lots if not lot.get("closedAt") and decimal(lot.get("size", 0), "parcela") > 0]


def add_hedge_lot(
    lots: list[dict[str, Any]],
    size: Decimal,
    entry_price: Decimal,
    reference_price: Decimal | None = None,
) -> dict[str, Any] | None:
    """Registra um aumento real do short como parcela interna independente."""
    if size <= 0 or entry_price <= 0:
        return None
    lot = {
        "id": f"{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}-{len(lots) + 1}",
        "size": str(size),
        "entryPrice": str(entry_price),
        "estimatedFeeUsd": str(size * entry_price * ESTIMATED_TAKER_FEE_RATE),
        "estimatedSlippageUsd": str(
            abs(entry_price - reference_price) * size if reference_price and reference_price > 0 else Decimal("0")
        ),
        "openedAt": now_iso(),
        "closedAt": None,
        "recoveryArmed": True,
        "recoveryForceClose": False,
    }
    lots.append(lot)
    return lot


def consume_hedge_lot(lot: dict[str, Any], filled_size: Decimal) -> Decimal:
    """Aplica uma compra reduce-only à parcela LIFO e devolve eventual sobra."""
    size = decimal(lot.get("size", 0), "parcela")
    consumed = min(size, max(Decimal("0"), filled_size))
    remaining = size - consumed
    lot["size"] = str(remaining)
    if remaining <= 0:
        lot["closedAt"] = now_iso()
    return filled_size - consumed


def lot_recovery_crossed(previous_mark: Decimal, mark: Decimal, entry_price: Decimal) -> bool:
    """Fecha a parcela no primeiro cruzamento ascendente de sua saída.

    O preço pode atravessar a faixa de 0,10% entre duas consultas. Por isso,
    não exigimos permanência dentro dela: basta vir de baixo e alcançar ou
    ultrapassar o piso de saída. Uma passagem descendente nunca dispara a
    recompra, preservando o hedge durante a queda.
    """
    if previous_mark <= 0 or mark <= 0 or entry_price <= 0:
        return False
    close_floor = entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
    return previous_mark < close_floor <= mark


def recovery_reentry_signal(mark: Decimal, recovery_high: Decimal, step: Decimal) -> bool:
    """Confirma reversão de meia banda usando uma única máxima global."""
    return (
        mark > 0
        and recovery_high > 0
        and step > 0
        and mark <= recovery_high * (Decimal("1") - step / Decimal("2"))
    )


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
        # O slot 1 preserva os dados já usados pelo app. Os demais slots têm
        # arquivos próprios: configuração, estado e registro jamais se misturam.
        self.config_file = CONFIG_FILE if slot == "1" else DATA_DIR / f"config-{slot}.json"
        self.log_file = LOG_FILE if slot == "1" else DATA_DIR / f"events-{slot}.jsonl"
        self.strategy_state_file = DATA_DIR / ("strategy-state.json" if slot == "1" else f"strategy-state-{slot}.json")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.execution_lock = threading.Lock()
        self.manual_stop_requested = False
        self.config = self._load_config()
        self.persisted_strategy = self._load_strategy_state()
        self.state: dict[str, Any] = {
            "mode": "stopped",
            "message": "Pronto para iniciar",
            "updatedAt": now_iso(),
            "snapshot": None,
        }

    def _load_strategy_state(self) -> dict[str, Any] | None:
        try:
            stored = json.loads(self.strategy_state_file.read_text(encoding="utf-8"))
            return stored if isinstance(stored, dict) and stored.get("version") in {1, 2, 3, 4, 5, 6} else None
        except (OSError, json.JSONDecodeError):
            return None

    def _clear_strategy_state(self) -> None:
        self.persisted_strategy = None
        try:
            self.strategy_state_file.unlink()
        except FileNotFoundError:
            pass

    def _persist_strategy_state(self, snapshot: dict[str, Any]) -> None:
        """Salva apenas o estado necessário para retomar a estratégia.

        O arquivo é substituído atomicamente. Assim, uma queda de energia não
        deixa JSON parcial e uma atualização do mark, por si só, não desgasta
        o armazenamento do Umbrel.
        """
        # Uma parada manual encerra deliberadamente a operação. Mesmo que uma
        # iteração já estivesse em andamento, ela não pode recriar a referência
        # que acabou de ser apagada pelo usuário.
        if self.manual_stop_requested:
            return
        position = snapshot.get("position") or {}
        payload = {
            "version": 6,
            "source": self.config.get("source"),
            "positionAddress": position.get("positionAddress"),
            "market": snapshot.get("market"),
            "hyperliquidAccount": self.config.get("hyperliquidAccount"),
            "hedgeStrategy": snapshot.get("hedgeStrategy"),
            "hedgeRegime": snapshot.get("hedgeRegime"),
            "protectionReference": str(snapshot.get("protectionReference")),
            "realShort": str(snapshot.get("realShort")),
            "baseShort": str(snapshot.get("baseShort", snapshot.get("realShort", 0))),
            "baseRecoveryArmed": bool(snapshot.get("baseRecoveryArmed", False)),
            "baseRecoveryForceClose": bool(snapshot.get("baseRecoveryForceClose", False)),
            "hedgeLots": snapshot.get("hedgeLots", []),
            "recoveryActive": bool(snapshot.get("recoveryActive", False)),
            "recoveryHigh": str(snapshot.get("recoveryHigh", 0)),
        }
        previous = self.persisted_strategy or {}
        if all(previous.get(key) == value for key, value in payload.items()):
            return
        stored = {**payload, "updatedAt": now_iso()}
        temporary = self.strategy_state_file.with_suffix(self.strategy_state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.strategy_state_file)
        self.persisted_strategy = stored

    def _restore_strategy_state(
        self,
        position: dict[str, Any],
        hyp: HypState,
        hedge_strategy: str,
    ) -> tuple[str, Decimal] | None:
        stored = self.persisted_strategy
        if not stored or hedge_strategy != "upside":
            return None
        identity_matches = (
            stored.get("source") == self.config.get("source")
            and stored.get("positionAddress") == position.get("positionAddress")
            and str(stored.get("market", "")).upper() == hyp.market.upper()
            and str(stored.get("hyperliquidAccount", "")).lower()
            == self.config.get("hyperliquidAccount", "").lower()
            and stored.get("hedgeStrategy") == hedge_strategy
        )
        if not identity_matches:
            return None
        try:
            regime = str(stored.get("hedgeRegime"))
            reference = decimal(stored.get("protectionReference"), "referência persistida")
        except NeutralisError:
            return None
        if regime not in {"protected", "upside", "initial_wait", "direction_wait"} or reference <= 0:
            return None
        # Migração pontual da operação NEAR que já estava aberta
        # quando a referência fixa passou a ser persistida. O estado antigo
        # registrou 4,10, mas a referência correta desta operação é 4,20.
        # A versão 5 gravada logo depois impede que a correção se repita ou
        # afete uma operação futura que legitimamente comece perto de 4,10.
        state_version = int(stored.get("version", 1))
        if (
            state_version <= 4
            and hyp.market.upper() == "NEAR"
            and reference.quantize(Decimal("0.01")) == Decimal("4.10")
        ):
            previous_reference = reference
            reference = Decimal("4.20")
            self._event(
                "reference-correction",
                "Referência inicial da operação NEAR corrigida uma única vez para US$ 4,20",
                previousReference=previous_reference,
                reference=reference,
                market=hyp.market,
            )
        # Correção única das duas operações que já estavam abertas quando a
        # referência inicial passou a ser gravada corretamente. Somente um
        # estado da versão 5 e com o preço antigo específico entra aqui. Ao
        # persistir novamente, a versão 6 impede repetição e protege operações
        # futuras que legitimamente tenham esses mesmos preços.
        one_time_reference_corrections = {
            "AVAX": (Decimal("11.235"), Decimal("11.245"), Decimal("11.08")),
            "NEAR": (Decimal("4.275"), Decimal("4.285"), Decimal("4.10")),
        }
        correction = one_time_reference_corrections.get(hyp.market.upper())
        if state_version == 5 and correction:
            lower_old, upper_old, corrected_reference = correction
            if lower_old <= reference < upper_old:
                previous_reference = reference
                reference = corrected_reference
                # As duas posições reais já estão acima da referência correta.
                # Armar a recuperação faz o início reconciliar e zerar o short,
                # em vez de conservar indevidamente o regime protegido antigo.
                stored["baseRecoveryArmed"] = True
                stored["baseRecoveryForceClose"] = True
                self._event(
                    "reference-correction",
                    f"Referência inicial da operação {hyp.market.upper()} corrigida uma única vez para US$ {corrected_reference}",
                    previousReference=previous_reference,
                    reference=reference,
                    market=hyp.market,
                )
        actual_is_protected = hyp.signed_position < 0
        saved_is_protected = regime == "protected"
        if saved_is_protected != actual_is_protected:
            self._event(
                "state-reconciliation",
                "Estado salvo divergia da posição real; prevaleceu a Hyperliquid",
                savedRegime=regime,
                actualRegime="protected" if actual_is_protected else "unprotected",
                market=hyp.market,
            )
            return None
        self._event(
            "state-restored",
            "Estado da estratégia recuperado com segurança",
            regime=regime,
            reference=reference,
            market=hyp.market,
        )
        return regime, reference

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
            "initialPrincipalUsd": "",
            "hedgeStrategy": "upside",
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
        # A interface não preenche novamente campos sensíveis/operacionais
        # durante a atualização periódica. Na Arc o Token ID é obrigatório;
        # portanto um envio vazio não deve apagar o NFT já salvo.
        if not uniswap_token_id and source == "uniswap_arc":
            uniswap_token_id = str(self.config.get("uniswapTokenId", "")).strip()
        account = str(incoming.get("hyperliquidAccount", self.config["hyperliquidAccount"]))
        position = str(incoming.get("positionAddress", self.config["positionAddress"]))
        max_notional = decimal(incoming.get("maxPositionNotional", self.config["maxPositionNotional"]), "limite máximo do short")
        step_percent = decimal(incoming.get("stepPercent", self.config["stepPercent"]), "gatilho de ajuste")
        initial_principal_raw = str(incoming.get("initialPrincipalUsd", self.config.get("initialPrincipalUsd", ""))).strip()
        initial_principal = decimal(initial_principal_raw, "saldo inicial da LP") if initial_principal_raw else None
        hedge_strategy = str(incoming.get("hedgeStrategy", self.config.get("hedgeStrategy", "upside"))).lower()
        evm_source = source in {"uniswap", "uniswap_arc"}
        if source not in {"byreal", "raydium", "orca", "uniswap", "uniswap_arc"}:
            raise NeutralisError("Fonte de liquidez inválida")
        if not evm_source and not SOLANA_PATTERN.fullmatch(wallet):
            raise NeutralisError("Carteira Solana inválida")
        if evm_source and not EVM_PATTERN.fullmatch(evm_wallet):
            raise NeutralisError("Carteira EVM inválida")
        if not EVM_PATTERN.fullmatch(account):
            raise NeutralisError("Conta Hyperliquid inválida")
        if evm_source and position and not (
            EVM_PATTERN.fullmatch(position) or re.fullmatch(r"0x[0-9a-fA-F]{64}", position)
        ):
            raise NeutralisError("Pool Uniswap inválida; use o endereço V3 ou o Pool ID V4")
        if evm_source and uniswap_token_id and not re.fullmatch(r"[1-9][0-9]{0,77}", uniswap_token_id):
            raise NeutralisError("NFT Uniswap inválido; use somente o número do Token ID")
        if not evm_source and position and not SOLANA_PATTERN.fullmatch(position):
            raise NeutralisError("Endereço da posição inválido")
        if source == "uniswap_arc" and not uniswap_token_id:
            raise NeutralisError("Informe o Token ID numérico do NFT Uniswap na Arc")
        if not Decimal("10") <= max_notional <= Decimal("100000"):
            raise NeutralisError("O limite máximo do short deve ficar entre US$ 10 e US$ 100.000")
        if not Decimal("0.05") <= step_percent <= Decimal("5"):
            raise NeutralisError("O gatilho de ajuste deve ficar entre 0,05% e 5,00%")
        if initial_principal is not None and not Decimal("1") <= initial_principal <= Decimal("100000000"):
            raise NeutralisError("O saldo inicial da LP deve ficar entre US$ 1 e US$ 100.000.000")
        if hedge_strategy not in {"neutral", "upside"}:
            raise NeutralisError("Estratégia de hedge inválida")
        with self.lock:
            if self.state["mode"] == "running":
                raise NeutralisError("Pare o monitor antes de alterar a configuração")
            previous_identity = tuple(
                self.config.get(key, "")
                for key in ("source", "solanaWallet", "evmWallet", "uniswapTokenId", "hyperliquidAccount", "positionAddress")
            )
            self.config = {"source": source, "solanaWallet": wallet, "evmWallet": evm_wallet, "uniswapTokenId": uniswap_token_id, "hyperliquidAccount": account, "positionAddress": position, "maxPositionNotional": str(max_notional), "stepPercent": str(step_percent), "initialPrincipalUsd": str(initial_principal) if initial_principal is not None else "", "hedgeStrategy": hedge_strategy}
            self.config_file.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
            os.chmod(self.config_file, 0o600)
            current_identity = tuple(
                self.config.get(key, "")
                for key in ("source", "solanaWallet", "evmWallet", "uniswapTokenId", "hyperliquidAccount", "positionAddress")
            )
            if current_identity != previous_identity:
                self._clear_strategy_state()
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

    def _exchange(self, active_dex: str | None = None):
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
            perp_dexs=list(dict.fromkeys(["", "xyz", "mkts", active_dex or ""])),
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
        total_fill_notional = Decimal("0")
        last_direction: bool | None = None
        margin_size_cap: Decimal | None = None

        for attempt in count():
            if attempt:
                position, hyp, lower, upper, _, _ = self._retry_snapshot()
                if original_position and position.get("positionAddress") != original_position:
                    raise NeutralisError("A posição selecionada mudou durante o ajuste")

            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            if hyp.signed_position > 0:
                raise NeutralisError("A conta ficou long; monitor pausado")
            current_short = abs(min(hyp.signed_position, Decimal("0")))
            quantum = Decimal(1).scaleb(-hyp.decimals)
            difference = target - current_short
            size = abs(difference).quantize(quantum, rounding=ROUND_DOWN)
            if margin_size_cap is not None and difference > 0:
                size = min(size, margin_size_cap).quantize(quantum, rounding=ROUND_DOWN)
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
                    "averageFillPrice": total_fill_notional / total_filled if total_filled else hyp.mark,
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
                    limit_price = hyp_ioc_limit_price(hyp.mark, slippage, is_buy, hyp.decimals)
                    response = self._exchange(hyp.dex).order(
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
                insufficient_margin = "INSUFFICIENT MARGIN" in error_text
                if insufficient_margin and not is_buy:
                    # Não pause todo o monitor por falta de margem. Reduza a
                    # ordem progressivamente até encontrar o tamanho aceito.
                    # Se nem a ordem mínima couber, mantenha o robô vivo para
                    # tentar novamente no próximo ciclo ou após novo depósito.
                    margin_size_cap = (size / Decimal("2")).quantize(quantum, rounding=ROUND_DOWN)
                    if margin_size_cap <= 0 or margin_size_cap * hyp.mark < AUTO_MIN_ORDER_NOTIONAL:
                        self._event(
                            "margin-limited",
                            "Margem insuficiente; ajuste pendente e monitor mantido ativo",
                            requestedSize=size,
                            target=target,
                            mark=hyp.mark,
                            residualNotional=residual_notional,
                        )
                        if total_filled:
                            return {
                                "size": total_filled,
                                "notional": total_fill_notional,
                                "isBuy": last_direction,
                                "filled": total_filled,
                                "residualNotional": residual_notional,
                                "currentShort": current_short,
                                "target": target,
                                "anchor": hyp.mark,
                                "averageFillPrice": total_fill_notional / total_filled,
                            }
                        return None
                    self._event(
                        "margin-retry",
                        f"Margem insuficiente; tentando ordem menor de {margin_size_cap} {position['hedgeSymbol']}",
                        requestedSize=size,
                        reducedSize=margin_size_cap,
                        target=target,
                        mark=hyp.mark,
                    )
                    if self.stop_event.wait(AUTO_RETRY_SECONDS):
                        raise NeutralisError("Monitor interrompido durante o ajuste")
                    continue
                retryable = (
                    "IOC" in error_text
                    or "IOCCANCEL" in error_text
                    or "COULD NOT IMMEDIATELY MATCH" in error_text
                    or "RESTING ORDERS" in error_text
                    or self._is_transient_network_error(error)
                )
                if not retryable:
                    raise
                attempt_number = attempt + 1
                max_attempt = len(AUTO_RETRY_SLIPPAGES)
                # Registra o escalonamento e, no patamar máximo, uma vez a
                # cada dez tentativas para manter o histórico legível.
                if attempt_number <= max_attempt or attempt_number % 10 == 0:
                    suffix = (
                        "conexão temporariamente indisponível; mantendo o monitor ativo"
                        if self._is_transient_network_error(error)
                        else f"tentando novamente com slippage máximo de {slippage * 100:.2f}%"
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
            fill_price = decimal(filled.get("avgPx", hyp.mark), "preço executado")
            total_filled += filled_size
            total_fill_notional += filled_size * fill_price
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
        if self.config["source"] == "uniswap_arc":
            token_id = int(self.config["uniswapTokenId"])
            owner = arc_uniswap_owner(token_id)
            if owner.lower() != self.config["evmWallet"].lower():
                raise NeutralisError("O NFT Uniswap da Arc não pertence à carteira EVM informada")
            position = arc_uniswap_v4_position(token_id, self.config["positionAddress"])
            if position is None:
                raise NeutralisError(
                    "NFT Uniswap da Arc sem liquidez ou não correspondente ao Pool ID informado"
                )
            return [position]
        if self.config["source"] == "uniswap":
            pool_id = self.config["positionAddress"]
            if not pool_id:
                return []
            token_id = self.config.get("uniswapTokenId", "")
            is_v3 = bool(EVM_PATTERN.fullmatch(pool_id))
            if token_id:
                position = (
                    uniswap_v3_position(int(token_id), pool_id)
                    if is_v3 else uniswap_v4_position(int(token_id), pool_id)
                )
                if position is None:
                    raise NeutralisError(
                        "NFT Uniswap não corresponde a esta pool, está sem liquidez ou não pôde ser lido pelo RPC. "
                        "Confirme o Token ID numérico do NFT da pool selecionada."
                    )
                return [position]
            positions = (
                uniswap_v3_positions(self.config["evmWallet"], pool_id)
                if is_v3 else uniswap_v4_positions(self.config["evmWallet"], pool_id)
            )
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
        # Algumas respostas da Byreal não incluem tickCurrent. Nessa situação
        # o mark da Hyp é uma referência válida para manter o cálculo da LP e
        # o hedge em funcionamento, em vez de transformar None em erro.
        lp_price = decimal(position.get("currentPrice") or hyp.mark, "preço da LP")
        if position.get("normalizedLiquidity") is not None:
            liquidity = decimal(position["normalizedLiquidity"], "normalizedLiquidity")
        else:
            liquidity = lp_liquidity(value, lp_price, lower, upper)
        target = target_at_reference_price(position, liquidity, lp_price, lower, upper, hyp.mark)
        return position, hyp, lower, upper, liquidity, target

    @staticmethod
    def _is_transient_network_error(error: NeutralisError) -> bool:
        return str(error).startswith("Falha de rede")

    @staticmethod
    def _is_temporarily_unavailable_position(error: NeutralisError) -> bool:
        return str(error) == "A posição selecionada não está aberta ou não pode ser calculada"

    def _retry_snapshot(self) -> tuple[dict[str, Any], HypState, Decimal, Decimal, Decimal, Decimal]:
        """Aguarda uma leitura utilizável sem encerrar um hedge já ativo."""
        failures = 0
        retry_kind = "network"
        while not self.stop_event.is_set():
            try:
                snapshot = self._live_snapshot()
                if failures:
                    if retry_kind == "position":
                        self._event(
                            "position-recovered",
                            f"Leitura da posição restabelecida após {failures} tentativa(s)",
                        )
                    else:
                        self._event("network-recovered", f"Conexão restabelecida após {failures} tentativa(s)")
                return snapshot
            except NeutralisError as error:
                is_network = self._is_transient_network_error(error)
                is_position = self._is_temporarily_unavailable_position(error)
                if not (is_network or is_position):
                    raise
                retry_kind = "position" if is_position else "network"
                failures += 1
                if failures == 1 or failures % 12 == 0:
                    event = "position-retry" if is_position else "network-retry"
                    message = (
                        f"Leitura temporariamente indisponível; tentando novamente ({failures})"
                        if is_position
                        else f"{error}; tentando novamente ({failures})"
                    )
                    self._event(event, message, reason=str(error))
                if self.stop_event.wait(5):
                    break
        raise NeutralisError("Monitor interrompido pelo usuário")

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
            # A parada manual deixa o evento sinalizado para encerrar a thread.
            # Limpe-o antes da validação do próximo início; o modo real usa
            # _retry_snapshot() nessa fase e não deve interpretar o sinal
            # antigo como uma nova interrupção do usuário.
            self.manual_stop_requested = False
            self.stop_event.clear()
            if live:
                position, hyp, _, _, _, _ = self._retry_snapshot()
                expected = "ATIVAR"
                if confirmation.strip().upper() != expected:
                    raise NeutralisError("Confirmação incorreta. Digite exatamente: ATIVAR")
                if not API_KEY_FILE.exists():
                    raise NeutralisError("Cadastre a chave da API Wallet antes de ativar o modo real")
                if position["hedgeSymbol"] != hyp_symbol(position["assetSymbol"]):
                    raise NeutralisError("Mapeamento do contrato não pôde ser validado")
            self.state.update({"mode": "starting", "message": "Validando fontes de dados", "updatedAt": now_iso()})
            self.thread = threading.Thread(target=self._run, args=(live,), name="neutralis-live" if live else "neutralis-dry-run", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.manual_stop_requested = True
            # Parar manualmente encerra a operação estratégica. No próximo
            # início, o mark daquele momento será a nova referência fixa.
            self._clear_strategy_state()
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
            position, hyp, lower, upper, liquidity, target = self._retry_snapshot()
            step = self.rebalance_step(lower, upper)
            if hyp.signed_position > 0:
                raise NeutralisError("A conta está long; o monitor exige posição zero ou short")
            if live and abs(hyp.signed_position) * hyp.mark > self.max_position_notional():
                raise NeutralisError(f"O short real já ultrapassa o limite total de US$ {self.max_position_notional():.2f}")
            if hyp.open_orders:
                raise NeutralisError("Existem ordens abertas neste mercado")
            lp_price = decimal(position.get("currentPrice") or hyp.mark, "preço da LP")
            # A Hyp é o mercado contínuo que determina quando o hedge deve
            # reagir. A Orca permanece como fonte da faixa e da liquidez.
            lp_anchor = lp_price
            hyp_anchor = hyp.mark
            ratio_anchor = lp_anchor / hyp_anchor
            hedge_strategy = self.config.get("hedgeStrategy", "upside")
            current_short = abs(min(hyp.signed_position, Decimal("0")))
            # Sem short e sem histórico desta operação, observe primeiro para
            # qual lado o mercado anda. Um estado salvo distingue essa estreia
            # de uma proteção que já foi fechada deliberadamente na alta.
            hedge_regime = "protected" if current_short > 0 else "initial_wait"
            # A fronteira estratégica nasce no primeiro início da operação e
            # permanece fixa. O preço médio móvel do short nunca a substitui.
            protection_reference = hyp.mark
            # O dry-run continua isolado: somente o modo real recupera o
            # estado operacional salvo antes de uma reinicialização.
            restored_strategy = self._restore_strategy_state(position, hyp, hedge_strategy) if live else None
            if restored_strategy:
                hedge_regime, protection_reference = restored_strategy
            hedge_lots: list[dict[str, Any]] = []
            recovery_active = False
            recovery_high = Decimal("0")
            base_recovery_armed = False
            base_recovery_force_close = False
            if live and restored_strategy and self.persisted_strategy:
                state_version = int(self.persisted_strategy.get("version", 1))
                # Estados antigos não registravam a passagem do short-base
                # abaixo do piso. Uma posição protegida já existente migra
                # armada para encerrar imediatamente uma recuperação perdida.
                base_recovery_armed = (
                    bool(self.persisted_strategy.get("baseRecoveryArmed", False))
                    if state_version >= 4
                    else hedge_regime == "protected" and current_short > 0
                )
                base_recovery_force_close = (
                    bool(self.persisted_strategy.get("baseRecoveryForceClose", False))
                    if state_version >= 4
                    else hedge_regime == "protected" and current_short > 0
                )
            if (
                hedge_regime == "protected"
                and hyp.entry_price > 0
                and hyp.mark < hyp.entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
            ):
                base_recovery_armed = True
            if live and restored_strategy and self.persisted_strategy and self.persisted_strategy.get("version") in {2, 3, 4, 5, 6}:
                raw_lots = self.persisted_strategy.get("hedgeLots", [])
                if isinstance(raw_lots, list):
                    for raw_lot in raw_lots:
                        if not isinstance(raw_lot, dict):
                            continue
                        try:
                            if decimal(raw_lot.get("size", 0), "parcela") > 0 and decimal(raw_lot.get("entryPrice", 0), "entrada da parcela") > 0:
                                migrated_lot = dict(raw_lot)
                                # Parcelas criadas antes da versão 3 não
                                # registravam a passagem por baixo do piso.
                                # Migram armadas para não ficarem presas após
                                # a atualização que interrompeu o cruzamento.
                                if "recoveryArmed" not in migrated_lot:
                                    migrated_lot["recoveryArmed"] = True
                                    migrated_lot["recoveryForceClose"] = True
                                elif "recoveryForceClose" not in migrated_lot:
                                    migrated_lot["recoveryForceClose"] = False
                                hedge_lots.append(migrated_lot)
                        except NeutralisError:
                            continue
                recovery_active = bool(self.persisted_strategy.get("recoveryActive", False))
                try:
                    recovery_high = decimal(self.persisted_strategy.get("recoveryHigh", 0), "máxima da recuperação")
                except NeutralisError:
                    recovery_high = Decimal("0")
            lots_total = sum((decimal(lot["size"], "parcela") for lot in open_hedge_lots(hedge_lots)), Decimal("0"))
            # A posição real é a fonte de verdade. Uma diferença após execução
            # manual ou migração fica incorporada ao short-base, nunca cria uma
            # parcela fictícia que o robô poderia encerrar indevidamente.
            base_short = max(Decimal("0"), current_short - lots_total)
            if lots_total > current_short:
                hedge_lots = []
                base_short = current_short
                recovery_active = False
                recovery_high = Decimal("0")
                self._event("lot-reconciliation", "Parcelas salvas divergiam do short real; posição real preservada como base")
            regime_confirmation: str | None = None
            regime_confirmation_count = 0
            initial_adjusted = False
            initial_target = target
            if hedge_strategy == "upside":
                # Sem short, começa participando da alta e só abre proteção
                # depois da queda confirmada. Se o short já estiver acima da
                # banda de saída, não aumente a posição antes de fechá-la.
                if hedge_regime in {"upside", "initial_wait", "direction_wait"}:
                    initial_target = Decimal("0")
                elif upside_hedge_signal(
                    hedge_regime,
                    hyp.mark,
                    protection_reference,
                    step,
                    hyp.entry_price,
                    base_recovery_armed and base_recovery_force_close,
                ) == "close":
                    initial_target = current_short
            initial_residual = abs(initial_target - current_short) * hyp.mark
            restored_lot_reduction = bool(
                restored_strategy and open_hedge_lots(hedge_lots) and initial_target < current_short
            )
            if live and initial_residual >= AUTO_MIN_ORDER_NOTIONAL and not restored_lot_reduction:
                self._event(
                    "initial-reconciliation",
                    f"CORRIGIR DELTA INICIAL · residual US$ {initial_residual:.2f}",
                    currentShort=current_short,
                    target=initial_target,
                    mark=hyp.mark,
                )
                result = self._execute_auto_adjustment(position, hyp, initial_target)
                initial_adjusted = bool(result)
                initial_difference = initial_target - current_short
                position, hyp, lower, upper, liquidity, target = self._retry_snapshot()
                lp_price = decimal(position.get("currentPrice") or hyp.mark, "preço da LP")
                lp_anchor = lp_price
                hyp_anchor = hyp.mark
                ratio_anchor = lp_anchor / hyp_anchor
                if result and initial_difference > 0 and restored_strategy:
                    add_hedge_lot(
                        hedge_lots,
                        decimal(result["filled"], "execução da parcela"),
                        decimal(result.get("averageFillPrice", hyp.mark), "preço executado"),
                        hyp.mark,
                    )
                else:
                    base_short = abs(min(hyp.signed_position, Decimal("0")))
                    hedge_lots = []
                    recovery_active = False
                    recovery_high = Decimal("0")
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
                "targetShort": initial_target,
                "anchor": anchor,
                "lower": lower,
                "upper": upper,
                "stepPercent": step * 100,
                "hedgeStrategy": hedge_strategy,
                "hedgeRegime": hedge_regime,
                "protectionReference": protection_reference,
                "protectionDistancePercent": (hyp.mark / protection_reference - Decimal("1")) * Decimal("100"),
                **principal_metrics(position, self.config.get("initialPrincipalUsd", "")),
                "baseShort": base_short,
                "baseRecoveryArmed": base_recovery_armed,
                "baseRecoveryForceClose": base_recovery_force_close,
                "hedgeLots": hedge_lots,
                "openLotCount": len(open_hedge_lots(hedge_lots)),
                "recoveryActive": recovery_active,
                "recoveryHigh": recovery_high,
                "closeThreshold": (
                    protection_reference * (Decimal("1") + step / Decimal("2"))
                    if hedge_regime in {"initial_wait", "direction_wait"}
                    else hyp.entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
                    if hedge_regime == "protected" and hyp.entry_price > 0
                    else protection_reference
                ),
                "openThreshold": protection_reference * (
                    Decimal("1") - step / Decimal("2")
                    if hedge_regime in {"initial_wait", "direction_wait"}
                    else Decimal("1")
                ),
                "live": live,
                "pendingNotional": abs(initial_target - virtual_short) * hyp.mark,
            }
            with self.lock:
                label = "MODO REAL ativo" if live else "Dry-run ativo"
                regime_label = {
                    "protected": "protegido",
                    "initial_wait": "aguardando direção",
                    "direction_wait": "aguardando nova direção",
                    "upside": "sem short · aguardando retorno à referência",
                }.get(hedge_regime, hedge_regime)
                self.state.update({"mode": "running", "message": f"{label} · {regime_label} · banda de {step * 100:.2f}%", "snapshot": json_safe(initial_snapshot), "updatedAt": now_iso()})
            if live:
                self._persist_strategy_state(initial_snapshot)
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
            previous_hyp_mark = hyp.mark

            while not self.stop_event.wait(AUTO_POLL_SECONDS):
                position_now, hyp_now, lower, upper, liquidity, target = self._retry_snapshot()
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
                lp_price = decimal(position_now.get("currentPrice") or hyp_now.mark, "preço da LP")
                basis_from_anchor = hedge_basis(position_now, lp_price, hyp_now.mark, ratio_anchor)

                # A projeção usa a relação LP/Hyp registrada na última
                # âncora. É ela que alimenta a fórmula CLMM entre swaps na
                # Orca; o gatilho é exclusivamente o mark da Hyperliquid.
                movement = hyp_now.mark / hyp_anchor - Decimal("1")
                projected_lp_price = lp_anchor * hyp_now.mark / hyp_anchor
                target = target_at_reference_price(position_now, liquidity, projected_lp_price, lower, upper, hyp_now.mark)
                full_hedge_target = target
                if hedge_strategy == "upside":
                    if hedge_regime == "protected" and hyp_now.entry_price > 0:
                        base_close_floor = hyp_now.entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
                        if hyp_now.mark < base_close_floor:
                            base_recovery_armed = True
                    signal = upside_hedge_signal(
                        hedge_regime,
                        hyp_now.mark,
                        protection_reference,
                        step,
                        hyp_now.entry_price,
                        base_recovery_armed
                        and (base_recovery_force_close or hyp_now.mark > previous_hyp_mark),
                    )
                    # Depois de zerar o short-base, a reentrada é controlada
                    # exclusivamente pela reversão de meia banda desde a máxima
                    # da recuperação, e não pela referência inicial.
                    if hedge_regime == "upside" and recovery_active:
                        signal = None
                    # As parcelas adicionais são recuperadas primeiro, em
                    # ordem LIFO. O short-base só pode ser zerado depois que
                    # nenhuma parcela adicional continuar aberta.
                    if signal == "close" and open_hedge_lots(hedge_lots):
                        signal = None
                    if signal is None:
                        # Ausência de troca de regime não é uma confirmação.
                        # O contador precisa ficar zerado para que o ajuste
                        # delta normal continue autorizado após o gatilho.
                        regime_confirmation = None
                        regime_confirmation_count = 0
                    elif signal == regime_confirmation:
                        regime_confirmation_count += 1
                    else:
                        regime_confirmation = signal
                        regime_confirmation_count = 1

                    # Abrir/voltar à espera exige duas leituras. O short-base
                    # armado encerra na primeira recuperação observada para
                    # não deixar o prejuízo crescer enquanto o ativo sobe.
                    required_confirmation = 1 if signal == "close" else 2
                    if signal and regime_confirmation_count >= required_confirmation:
                        next_target = Decimal("0") if signal in {"close", "confirm_upside", "wait"} else target
                        if live:
                            # Confirmar uma alta inicial não exige execução: a
                            # posição já está zerada. Nos demais sinais, ajuste
                            # normalmente e confirme o resultado on-chain.
                            if signal not in {"confirm_upside", "wait"}:
                                result = self._execute_auto_adjustment(position_now, hyp_now, next_target)
                                if result:
                                    virtual_short = result["currentShort"]
                                position_now, hyp_now, lower, upper, liquidity, target = self._retry_snapshot()
                                lp_price = decimal(position_now.get("currentPrice") or hyp_now.mark, "preço da LP")
                        else:
                            before = virtual_short
                            virtual_short = next_target
                            self._event(
                                "upside-close" if signal == "close" else "initial-upside" if signal == "confirm_upside" else "direction-wait" if signal == "wait" else "downside-open",
                                f"SIMULAR {'FECHAR TODO O SHORT' if signal == 'close' else 'CONFIRMAR ALTA' if signal == 'confirm_upside' else 'AGUARDAR NOVA DIREÇÃO' if signal == 'wait' else 'REABRIR HEDGE DE 100%'}",
                                before=before,
                                after=virtual_short,
                                mark=hyp_now.mark,
                                reference=protection_reference,
                                stepPercent=step * 100,
                            )
                        if signal == "close":
                            base_short = Decimal("0")
                            base_recovery_armed = False
                            base_recovery_force_close = False
                            hedge_lots = []
                            recovery_active = True
                            recovery_high = hyp_now.mark
                        elif signal == "open":
                            base_short = abs(min(hyp_now.signed_position, Decimal("0"))) if live else virtual_short
                            base_recovery_armed = True
                            base_recovery_force_close = False
                            hedge_lots = []
                            recovery_active = False
                            recovery_high = Decimal("0")
                        hedge_regime = (
                            "upside"
                            if signal in {"close", "confirm_upside"}
                            else "direction_wait"
                            if signal == "wait"
                            else "protected"
                        )
                        self._event(
                            "hedge-regime",
                            "Short zerado; participando da alta"
                            if signal == "close"
                            else "Alta confirmada; aguardando retorno à referência"
                            if signal == "confirm_upside"
                            else "Preço voltou à referência; aguardando nova direção"
                            if signal == "wait"
                            else "Queda confirmada; hedge de 100% reativado",
                            regime=hedge_regime,
                            mark=hyp_now.mark,
                            reference=protection_reference,
                            live=live,
                        )
                        lp_anchor, hyp_anchor, anchor = lp_price, hyp_now.mark, lp_price
                        ratio_anchor, movement, basis_from_anchor = lp_price / hyp_anchor, Decimal("0"), Decimal("0")
                        projected_lp_price = lp_anchor
                        regime_confirmation = None
                        regime_confirmation_count = 0

                    if hedge_regime in {"upside", "initial_wait", "direction_wait"}:
                        # Exibição e cálculo de pendência devem refletir que,
                        # nestes regimes, o alvo deliberado é zero.
                        target = Decimal("0")
                lot_action_performed = False
                # As vendas adicionais feitas durante a queda são parcelas
                # internas. Na recuperação, somente a parcela mais recente
                # elegível é reduzida (LIFO), perto do próprio preço de
                # execução. O fechamento ocorre no primeiro cruzamento
                # ascendente de 0,10% abaixo da entrada. Assim uma recuperação
                # rápida não escapa entre duas consultas.
                if hedge_regime == "protected" and open_hedge_lots(hedge_lots):
                    # Cada parcela fica permanentemente armada depois de uma
                    # leitura abaixo do próprio piso. Esse sinal faz parte do
                    # estado persistido e sobrevive a reinícios do container.
                    for open_lot in open_hedge_lots(hedge_lots):
                        open_entry = decimal(open_lot["entryPrice"], "entrada da parcela")
                        open_floor = open_entry * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
                        if hyp_now.mark < open_floor:
                            open_lot["recoveryArmed"] = True
                    newest_lot = open_hedge_lots(hedge_lots)[-1]
                    lot_entry = decimal(newest_lot["entryPrice"], "entrada da parcela")
                    lot_close_floor = lot_entry * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
                    crossed_recovery = lot_recovery_crossed(previous_hyp_mark, hyp_now.mark, lot_entry)
                    # Compatibilidade após reinício: se o processo voltar já
                    # dentro da faixa lucrativa, encerre a parcela sem esperar
                    # um novo mergulho. Acima da entrada, somente um cruzamento
                    # observado nesta execução autoriza a saída.
                    eligible = bool(newest_lot.get("recoveryArmed")) and hyp_now.mark >= lot_close_floor and (
                        bool(newest_lot.get("recoveryForceClose")) or hyp_now.mark > previous_hyp_mark
                    )
                    eligible = eligible or crossed_recovery
                    lot_id = str(newest_lot.get("id", ""))
                    if eligible:
                        lot_size = decimal(newest_lot["size"], "parcela")
                        current_for_lots = abs(min(hyp_now.signed_position, Decimal("0"))) if live else virtual_short
                        close_target = max(Decimal("0"), current_for_lots - lot_size)
                        if live:
                            result = self._execute_auto_adjustment(position_now, hyp_now, close_target)
                            if result:
                                consume_hedge_lot(newest_lot, decimal(result["filled"], "execução da parcela"))
                                virtual_short = result["currentShort"]
                        else:
                            consume_hedge_lot(newest_lot, lot_size)
                            virtual_short = close_target
                        self._event(
                            "lot-recovery",
                            f"PARCELA RECUPERADA · reduzir short {lot_size} {position_now['hedgeSymbol']}",
                            size=lot_size,
                            entryPrice=lot_entry,
                            mark=hyp_now.mark,
                            live=live,
                        )
                        recovery_active = True
                        recovery_high = hyp_now.mark
                        lot_action_performed = True

                # Depois de reduzir uma ou mais parcelas, uma reversão de
                # metade do gatilho a partir da máxima da recuperação recompõe
                # TODO o delta faltante em uma única ordem. A referência é
                # global, portanto o risco de meia banda nunca se acumula.
                if recovery_active and hedge_regime in {"protected", "upside"} and not lot_action_performed:
                    recovery_high = max(recovery_high, hyp_now.mark)
                    if recovery_reentry_signal(hyp_now.mark, recovery_high, step):
                        reentry_from_upside = hedge_regime == "upside"
                        reentry_completed = False
                        current_for_reentry = abs(min(hyp_now.signed_position, Decimal("0"))) if live else virtual_short
                        missing = max(Decimal("0"), full_hedge_target - current_for_reentry)
                        if missing * hyp_now.mark >= AUTO_MIN_ORDER_NOTIONAL:
                            if live:
                                result = self._execute_auto_adjustment(position_now, hyp_now, full_hedge_target)
                                if result:
                                    virtual_short = result["currentShort"]
                                    if hedge_regime == "upside":
                                        base_short = result["currentShort"]
                                        base_recovery_armed = True
                                        base_recovery_force_close = False
                                        hedge_lots = []
                                        reentry_completed = True
                                    else:
                                        add_hedge_lot(
                                            hedge_lots,
                                            decimal(result["filled"], "execução da parcela"),
                                            decimal(result.get("averageFillPrice", hyp_now.mark), "preço executado"),
                                            hyp_now.mark,
                                        )
                            else:
                                if hedge_regime == "upside":
                                    base_short = full_hedge_target
                                    base_recovery_armed = True
                                    base_recovery_force_close = False
                                    hedge_lots = []
                                    reentry_completed = True
                                else:
                                    add_hedge_lot(hedge_lots, missing, hyp_now.mark)
                                virtual_short += missing
                            self._event(
                                "base-reentry" if hedge_regime == "upside" else "lot-reentry",
                                f"REVERSÃO · recompor todo o delta faltante de {missing} {position_now['hedgeSymbol']}",
                                missing=missing,
                                recoveryHigh=recovery_high,
                                mark=hyp_now.mark,
                                live=live,
                            )
                        if reentry_from_upside and reentry_completed:
                            hedge_regime = "protected"
                        if not reentry_from_upside or reentry_completed:
                            recovery_active = False
                            recovery_high = Decimal("0")
                            lot_action_performed = True
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
                        base_short = Decimal("0")
                        base_recovery_armed = False
                        base_recovery_force_close = False
                        hedge_lots = []
                        recovery_active = False
                        recovery_high = Decimal("0")
                        if hedge_strategy == "upside":
                            hedge_regime = "upside"
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
                    if live and (hedge_strategy == "neutral" or hedge_regime == "protected"):
                        result = self._execute_auto_adjustment(position_now, hyp_now, target)
                        if result:
                            virtual_short = result["currentShort"]
                            base_short = result["currentShort"]
                    elif hedge_strategy == "neutral" or hedge_regime == "protected":
                        virtual_short = target
                        base_short = target
                    else:
                        target = Decimal("0")
                    lp_anchor, hyp_anchor, anchor = projected_lp_price, hyp_now.mark, projected_lp_price
                    ratio_anchor, movement, basis_from_anchor = lp_price / hyp_anchor, Decimal("0"), Decimal("0")
                    projected_lp_price = lp_anchor
                if abs(movement) >= step and (
                    hedge_strategy == "neutral"
                    or (hedge_regime == "protected" and regime_confirmation_count == 0)
                ) and not recovery_active and not lot_action_performed:
                    current_short = abs(min(hyp_now.signed_position, Decimal("0")))
                    difference = target - (current_short if live else virtual_short)
                    size = abs(difference).quantize(quantum, rounding=ROUND_DOWN)
                    notional = size * hyp_now.mark
                    # Uma redução correspondente a parcelas abertas espera a
                    # recuperação até o preço de entrada delas. Isso substitui
                    # a recompra automática uma banda acima, responsável pela
                    # perda repetida nas violinadas.
                    lot_managed_reduction = difference < 0 and bool(open_hedge_lots(hedge_lots))
                    if size > 0 and notional >= AUTO_MIN_ORDER_NOTIONAL and not lot_managed_reduction:
                        if live:
                            result = self._execute_auto_adjustment(position_now, hyp_now, target)
                            if result:
                                if difference > 0:
                                    add_hedge_lot(
                                        hedge_lots,
                                        decimal(result["filled"], "execução da parcela"),
                                        decimal(result.get("averageFillPrice", hyp_now.mark), "preço executado"),
                                        hyp_now.mark,
                                    )
                                else:
                                    base_short = result["currentShort"]
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
                            if difference > 0:
                                add_hedge_lot(hedge_lots, size, hyp_now.mark)
                            else:
                                base_short = virtual_short
                            self._event("adjustment", f"SIMULAR {action} {size} {position['hedgeSymbol']}", size=size, before=before, after=virtual_short, target=target, mark=hyp_now.mark)
                            lp_anchor = projected_lp_price
                            hyp_anchor = hyp_now.mark
                            ratio_anchor = lp_price / hyp_anchor
                            anchor = lp_anchor
                            movement = Decimal("0")
                            projected_lp_price = lp_anchor
                            basis_from_anchor = Decimal("0")
                    elif size > 0 and not lot_managed_reduction:
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
                    "hedgeStrategy": hedge_strategy,
                    "hedgeRegime": hedge_regime,
                    "protectionReference": protection_reference,
                    "protectionDistancePercent": (hyp_now.mark / protection_reference - Decimal("1")) * Decimal("100"),
                    **principal_metrics(position_now, self.config.get("initialPrincipalUsd", "")),
                    "baseShort": base_short,
                    "baseRecoveryArmed": base_recovery_armed,
                    "baseRecoveryForceClose": base_recovery_force_close,
                    "hedgeLots": hedge_lots,
                    "openLotCount": len(open_hedge_lots(hedge_lots)),
                    "recoveryActive": recovery_active,
                    "recoveryHigh": recovery_high,
                    "closeThreshold": (
                        protection_reference * (Decimal("1") + step / Decimal("2"))
                        if hedge_regime in {"initial_wait", "direction_wait"}
                        else hyp_now.entry_price * (Decimal("1") - BASE_RECOVERY_EXIT_BUFFER)
                        if hedge_regime == "protected" and hyp_now.entry_price > 0
                        else protection_reference
                    ),
                    "live": live,
                    "pendingNotional": abs(target - (abs(min(hyp_now.signed_position, Decimal('0'))) if live else virtual_short)) * hyp_now.mark,
                    "openThreshold": protection_reference * (
                        Decimal("1") - step / Decimal("2")
                        if hedge_regime in {"initial_wait", "direction_wait"}
                        else Decimal("1")
                    ),
                }
                with self.lock:
                    regime_label = {
                        "protected": "protegido",
                        "initial_wait": "aguardando direção",
                        "direction_wait": "aguardando nova direção",
                        "upside": "sem short · aguardando retorno à referência",
                    }.get(hedge_regime, hedge_regime)
                    self.state.update({
                        "message": f"{'MODO REAL' if live else 'Dry-run'} ativo · {regime_label} · banda de {step * 100:.2f}%",
                        "snapshot": json_safe(snapshot),
                        "updatedAt": now_iso(),
                    })
                if live:
                    self._persist_strategy_state(snapshot)
                previous_hyp_mark = hyp_now.mark
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


MONITORS = {slot: NeutralisMonitor(slot) for slot in ("1", "2", "3")}
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
