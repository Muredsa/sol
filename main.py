import asyncio
import os
import argparse
from decimal import Decimal, getcontext
import httpx
import json
import time
import base64
import struct

# Fallback import for different solana-py versions
try:
    from solana.rpc.async_api import AsyncClient
    from solana.keypair import Keypair
    from solana.transaction import Transaction
except ImportError:
    from solana.rpc.async_api import AsyncClient
    from solana.account import Account as Keypair
    from solana.transaction import Transaction

# Increase precision for financial calculations
getcontext().prec = 28

# Default parameters
DEFAULT_RPC_URL = os.getenv("SOLANA_RPC_URL", "ams60.nodes.rpcpool.com")
DEFAULT_PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH", os.path.expanduser("~/.config/solana/id.json"))
DEFAULT_BASE_TOKEN = os.getenv("BASE_TOKEN", "SOL")
DEFAULT_AMOUNT_IN = Decimal(os.getenv("AMOUNT_IN", "10"))
DEFAULT_MIN_PROFIT = Decimal(os.getenv("MIN_PROFIT", "0.01"))
DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
TOKEN_MINTS_CACHE_FILE = "token_mints_cache.json"
TOKEN_MINTS_CACHE_TTL = 60 * 60 * 24  # 24 часа
LIFINITY_PROGRAM_ID = "Lifinity8H1kTz5WQ6rRrF6rRrF6rRrF6rRrF6rRrF6rRrF6"  # проверьте актуальность!

async def load_keypair(path: str) -> Keypair:
    """Load user's keypair from JSON file"""
    import json
    with open(path) as f:
        data = json.load(f)
    return Keypair.from_secret_key(bytes(data))

def parse_lifinity_pool_data(data_b64):
    data = base64.b64decode(data_b64)
    # Примерный layout: 32 байта mint_a, 32 байта mint_b, 8 байт reserve_a, 8 байт reserve_b, 8 байт fee
    mint_a = data[0:32].hex()
    mint_b = data[32:64].hex()
    reserve_a = struct.unpack_from("<Q", data, 64)[0]
    reserve_b = struct.unpack_from("<Q", data, 72)[0]
    fee = struct.unpack_from("<Q", data, 80)[0]
    return {
        "token_a": mint_a,
        "token_b": mint_b,
        "reserve_a": Decimal(reserve_a),
        "reserve_b": Decimal(reserve_b),
        "fee": Decimal(fee) / Decimal("1000000"),  # пример, зависит от формата
        "dex": "lifinity"
    }

async def fetch_all_pools(client: AsyncClient) -> list:
    url = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
    try:
        async with httpx.AsyncClient() as http_client:
            print("DEBUG: отправляю запрос к Raydium API...")
            resp = await http_client.get(url, timeout=20)
            print(f"DEBUG: статус ответа Raydium: {resp.status_code}")
            print(f"DEBUG: первые 500 символов ответа: {resp.text[:500]}")
            if resp.status_code != 200:
                print(f"Ошибка загрузки пулов Raydium: {resp.status_code} {resp.text}")
                return []
            data = resp.json()
    except Exception as e:
        print(f"Ошибка при запросе Raydium: {e}")
        return []
    pools = []
    for pool in data.get("official", []) + data.get("unOfficial", []):
        try:
            pools.append({
                "token_a": pool["baseMint"],
                "token_b": pool["quoteMint"],
                "reserve_a": Decimal(pool["baseReserve"]),
                "reserve_b": Decimal(pool["quoteReserve"]),
                "fee": Decimal(pool.get("lpFeeRate", "0")) / Decimal("100"),
                "pool_mint": pool["lpMint"],
                "dex": "raydium"
            })
        except Exception as e:
            print(f"Ошибка парсинга пула: {e}")
            continue
    print(f"DEBUG: всего пулов Raydium: {len(pools)}")
    return pools


def simulate_swap(amount_in: Decimal, pool: dict, input_token: str) -> Decimal:
    fee = pool.get('fee', Decimal('0'))
    if input_token == pool['token_a']:
        reserve_in, reserve_out = pool['reserve_a'], pool['reserve_b']
    elif input_token == pool['token_b']:
        reserve_in, reserve_out = pool['reserve_b'], pool['reserve_a']
    else:
        return Decimal('0')
    amount_after_fee = amount_in * (1 - fee)
    return (amount_after_fee * reserve_out) / (reserve_in + amount_after_fee)


def find_arbitrage_opportunities(pools: list, base_token: str, amount_in: Decimal, min_profit: Decimal) -> list:
    opportunities = []
    pools_by_token = {}
    for p in pools:
        pools_by_token.setdefault(p['token_a'], []).append(p)
        pools_by_token.setdefault(p['token_b'], []).append(p)

    for p1 in pools_by_token.get(base_token, []):
        tok1 = p1['token_b'] if p1['token_a'] == base_token else p1['token_a']
        amt1 = simulate_swap(amount_in, p1, base_token)
        if amt1 <= 0:
            continue
        for p2 in pools_by_token.get(tok1, []):
            tok2 = p2['token_b'] if p2['token_a'] == tok1 else p2['token_a']
            if tok2 == base_token:
                continue
            amt2 = simulate_swap(amt1, p2, tok1)
            if amt2 <= 0:
                continue
            for p3 in pools_by_token.get(tok2, []):
                if base_token not in (p3['token_a'], p3['token_b']):
                    continue
                amt3 = simulate_swap(amt2, p3, tok2)
                profit = amt3 - amount_in
                if profit >= min_profit:
                    opportunities.append({
                        'path': [base_token, tok1, tok2, base_token],
                        'amount_out': amt3,
                        'profit': profit
                    })
    return opportunities

async def execute_route(client: AsyncClient, signer: Keypair, route: list, amount_in: Decimal):
    tx = Transaction()
    resp = await client.send_transaction(tx, signer)
    return resp

async def fetch_token_mints() -> dict:
    """Загрузка mint-адресов токенов из Solana Token List"""
    url = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()
    return {token["symbol"]: token["address"] for token in data["tokens"]}

async def get_token_mints_with_cache() -> dict:
    """Получить словарь mint-адресов токенов с кэшированием на диск (только Solana Token List)"""
    try:
        with open(TOKEN_MINTS_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("timestamp", 0) < TOKEN_MINTS_CACHE_TTL:
            return cache["token_mints"]
    except Exception:
        pass
    # Если кэш устарел или отсутствует — обновляем
    solana_token_mints = await fetch_token_mints()
    token_mints = solana_token_mints
    try:
        with open(TOKEN_MINTS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "token_mints": token_mints}, f)
    except Exception:
        pass
    return token_mints

async def main():
    parser = argparse.ArgumentParser(description="Solana Triangular Arbitrage Bot")
    parser.add_argument('--rpc-url', default=DEFAULT_RPC_URL)
    parser.add_argument('--keypair', default=DEFAULT_PRIVATE_KEY_PATH)
    parser.add_argument('--base-token', default=DEFAULT_BASE_TOKEN)
    parser.add_argument('--amount-in', type=Decimal, default=DEFAULT_AMOUNT_IN)
    parser.add_argument('--min-profit', type=Decimal, default=DEFAULT_MIN_PROFIT)
    parser.add_argument('--interval', type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument('--simulate', action='store_true', help='Run in simulation mode without loading a real wallet')
    args = parser.parse_args()

    client = AsyncClient(args.rpc_url)
    signer = None
    if not args.simulate:
        signer = await load_keypair(args.keypair)
    else:
        print("Simulation mode: skipping wallet load and transaction execution")

    print("Загружаю список токенов из Solana Token List (с кэшированием)...")
    token_mints = await get_token_mints_with_cache()

    # Преобразуем base_token в mint-адрес, если это тикер
    base_token = token_mints.get(args.base_token, args.base_token)

    print(f"Fetching pools from {args.rpc_url}...")
    pools = await fetch_all_pools(client)
    print(f"DEBUG: pools = {pools[:3]}")  # покажет первые 3 пула
    print(f"Loaded {len(pools)} pools")

    while True:
        opps = find_arbitrage_opportunities(pools, base_token, args.amount_in, args.min_profit)
        for opp in opps:
            print(f"Arb found: Path {opp['path']} -> Out {opp['amount_out']} Profit {opp['profit']}")
        await asyncio.sleep(args.interval)

if __name__ == '__main__':
    asyncio.run(main())