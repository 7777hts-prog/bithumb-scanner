import asyncio, os
from flask import Flask, jsonify
import httpx
from config import (
    BITHUMB_BASE, TOP_N_BY_VALUE, PREMIUM_MIN, ORDERBOOK_IMBAL_RATIO, 
    VOLUME_SURGE_RATIO, MA_COMPRESSION_MAX
)
from symbol_sync import build_intersection
from gate_stream import run_stream, STATE
from indicators import compression, final_score, adaptive_lead_threshold
from telegram_notify import send_telegram, can_send

app = Flask(__name__)
SYMBOL_MAP = {}
HTTP_TIMEOUT = 8.0

async def fetch_json(client, url, params=None):
    r = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

async def bithumb_all(client):
    return await fetch_json(client, f"{BITHUMB_BASE}/public/ticker/ALL_KRW")

async def candles_1h(client, sym):
    r = await fetch_json(client, f"{BITHUMB_BASE}/public/candlestick/{sym}_KRW/1h")
    return r.get("data", [])[-140:] if r.get("status") == "0000" else []

async def orderbook(client, sym):
    return await fetch_json(client, f"{BITHUMB_BASE}/public/orderbook/{sym}_KRW")

@app.get("/health")
def health(): 
    return {"ok": True, "ws_pairs": len(STATE.pairs), "mapped": len(SYMBOL_MAP)}

@app.get("/symbols")
def symbols():
    return {"mapped": SYMBOL_MAP}

@app.get("/scan")
async def scan():
    market_vps = sorted([m.get("vol_ps",0.0) for m in STATE.metrics.values()])
    vps_med = market_vps[len(market_vps)//2] if market_vps else 0.0
    LEAD_THRESH = adaptive_lead_threshold(vps_med)

    async with httpx.AsyncClient(headers={"accept":"application/json"}) as client:
        all_t = await bithumb_all(client)
        data = all_t.get("data", {})
        if not data:
            return jsonify({"ok": False}), 502
        usdt_krw = float(data.get("USDT",{}).get("closing_price", 0) or 0)

        rows = []
        for sym, row in data.items():
            if sym == "date": continue
            try:
                price = float(row["closing_price"]); value = float(row.get("acc_trade_value_24H",0))
            except: 
                continue
            if price>0 and value>0: rows.append((sym, price, value))
        rows.sort(key=lambda x: x[2], reverse=True)
        cand_syms = [s for s,_,_ in rows[:TOP_N_BY_VALUE]]

        tasks_c = {s: asyncio.create_task(candles_1h(client, s)) for s in cand_syms}
        tasks_o = {s: asyncio.create_task(orderbook(client, s)) for s in cand_syms}

        out = []
        for sym, price, value in rows[:TOP_N_BY_VALUE]:
            candles = await tasks_c[sym]
            close = [float(x[2]) for x in candles] if candles else []
            vol = [float(x[5]) for x in candles] if candles else []
            cmp_ratio = compression(close)
            vol_surge = (vol[-1] / (sum(vol[-6:-1])/5.0)) if len(vol)>=6 else 0.0

            ob = await tasks_o[sym]
            bid = sum(float(b["quantity"]) for b in ob.get("data", {}).get("bids", [])[:10])
            ask = sum(float(a["quantity"]) for a in ob.get("data", {}).get("asks", [])[:10])
            ob_ratio = (bid/ask) if ask>0 else 0.0

            prem = None
            gate_pair = SYMBOL_MAP.get(sym)
            if usdt_krw>0 and gate_pair and gate_pair in STATE.book:
                mid = (STATE.book[gate_pair]["best_bid"] + STATE.book[gate_pair]["best_ask"]) / 2.0
                if mid > 0:
                    prem = ( (price/usdt_krw) / mid ) - 1.0

            score, lead = final_score(gate_pair or "", prem, cmp_ratio)
            passed = bool(
                vol_surge >= VOLUME_SURGE_RATIO and
                ob_ratio >= ORDERBOOK_IMBAL_RATIO and
                (prem is not None and prem >= PREMIUM_MIN) and
                cmp_ratio <= MA_COMPRESSION_MAX
            )

            out.append({
                "symbol": sym,
                "price": price,
                "value_24h": round(value,0),
                "vol_surge": round(vol_surge,2),
                "orderbook_ratio": round(ob_ratio,2),
                "premium": None if prem is None else round(prem,4),
                "ma_compression": round(cmp_ratio,4),
                "lead": round(lead,3),
                "score": round(score,3),
                "pass": passed
            })

            if passed and lead >= LEAD_THRESH and prem is not None:
                key = f"{sym}"
                if can_send(key):
                    msg = (
                        f"🚀 <b>급등 감지</b> {sym}\n"
                        f"· score {score:.2f} / lead {lead:.2f}\n"
                        f"· premium {prem*100:.2f}%  · ob {ob_ratio:.2f}\n"
                        f"· 1h vol x{vol_surge:.2f} · MAcmp {cmp_ratio:.3f}\n"
                        f"· 확인: 4H 지지선/매물대 점검 후 진입"
                    )
                    await send_telegram(msg)

        out.sort(key=lambda x: (not x["pass"], -x["score"], -x["value_24h"]))
        return jsonify({"ok": True, "candidates": out[:50], "lead_thresh": round(LEAD_THRESH,2)})

@app.before_first_request
def _init_bg():
    loop = asyncio.get_event_loop()
    async def boot():
        global SYMBOL_MAP
        SYMBOL_MAP = await build_intersection()
        pairs = list(SYMBOL_MAP.values())
        loop.create_task(run_stream(pairs))
    loop.create_task(boot())

if __name__ == "__main__":
    port = int(os.getenv("PORT","8000"))
    app.run(host="0.0.0.0", port=port)
