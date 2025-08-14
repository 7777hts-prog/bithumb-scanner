import httpx
from typing import Dict, List
from config import BITHUMB_BASE, GATE_REST, MANUAL_SYMBOL_MAP

async def get_bithumb_symbols(client: httpx.AsyncClient) -> List[str]:
    r = await client.get(f"{BITHUMB_BASE}/public/ticker/ALL_KRW", timeout=8.0)
    r.raise_for_status()
    data = r.json().get("data", {})
    return [s for s in data.keys() if s not in ("date",)]

async def get_gate_usdt_pairs(client: httpx.AsyncClient) -> List[str]:
    r = await client.get(f"{GATE_REST}/spot/currency_pairs", timeout=10.0)
    r.raise_for_status()
    items = r.json()
    return [x["id"] for x in items if x.get("quote", "").upper() == "USDT"]

def map_symbol(sym: str, gate_pairs_set: set) -> str | None:
    if sym in MANUAL_SYMBOL_MAP:
        cand = f"{MANUAL_SYMBOL_MAP[sym]}_USDT"
        return cand if cand in gate_pairs_set else None
    cand = f"{sym}_USDT"
    if cand in gate_pairs_set: 
        return cand
    lower = f"{sym.lower()}_usdt"
    upper = f"{sym.upper()}_USDT"
    if lower in gate_pairs_set: 
        return lower
    if upper in gate_pairs_set: 
        return upper
    return None

async def build_intersection() -> Dict[str, str]:
    async with httpx.AsyncClient(headers={"accept": "application/json"}) as client:
        b_syms = await get_bithumb_symbols(client)
        g_pairs = await get_gate_usdt_pairs(client)
    gset = set(g_pairs)
    mapping = {}
    for s in b_syms:
        m = map_symbol(s, gset)
        if m:
            mapping[s] = m
    return mapping
