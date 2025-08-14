import asyncio, os, threading
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

# ---------- 유틸 ----------
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

def safe_ratio(num: float, den: float) -> float:
    try:
        if den and den > 0:
            return num / den
        return 0.0
    except Exception:
        return 0.0

# ---------- 안정 초기화(Flask 3.x) ----------
_init_done = False
_init_lock = threading.Lock()

def _start_ws_thread(pairs):
    async def _ws_main():
        await run_stream(pairs)
    t = threading.Thread(target=lambda: asyncio.run(_ws_main()), daemon=True)
    t.start()

def _blocking_init_once():
    global _init_done, SYMBOL_MAP
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        SYMBOL_MAP = asyncio.run(build_intersection())
        pairs = list(SYMBOL_MAP.values())
        if pairs:
            _start_ws_thread(pairs)
        _init_done = True

@app.before_request
def _ensure_initialized():
    _blocking_init_once()

# ---------- 라우트 ----------
@app.get("/health")
def health():
    return {"ok": True, "ws_pairs": len(STATE.pairs), "mapped": len(SYMBOL_MAP)}

@app.get("/symbols")
def symbols():
    return {"mapped": SYMBOL_MAP}

@app.get("/scan")
async def scan():
    market_vps = sorted([m.get("vol_ps", 0.0) for m in STATE.metrics.values()])
    vps_med = market_vps[len(market_vps)//2] if market_vps else 0.0
    LEAD_THRESH = adaptive_lead_threshold(vps_med)

    errors = []
    out = []

    async with httpx.AsyncClient(headers={"accept": "application/json"}) as client:
        try:
            all_t = await bithumb_all(client)
            data = all_t.get("data", {})
            if not data:
                return jsonify({"ok": False, "error": "bithumb empty"}), 502
            usdt_krw = float(data.get("USDT", {}).get("closing_price", 0) or 0)
        except Exception as e:
            return jsonify({"ok": False, "error": f"bithumb_all failed: {e}"}), 502

        rows = []
        for sym, row in data.items():
            if sym == "date":
                continue
            try:
                price = float(row.get("closing_price", 0) or 0)
                value = float(row.get("acc_trade_value_24H", 0) or 0)
                if price > 0 and value > 0:
                    rows.append((sym, price, value))
            except Exception:
                continue

        rows.sort(key=lambda x: x[2], reverse=True)
        cand_syms = [s for s, _, _ in rows[:TOP_N_BY_VALUE]]

        tasks_c = {s: asyncio.create_task(candles_1h(client, s)) for s in cand_syms}
        tasks_o = {s: asyncio.create_task(orderbook(client, s)) for s in cand_syms}

        for sym, price, value in rows[:TOP_N_BY_VALUE]:
            try:
                candles = await tasks_c[sym]
                close = [float(x[2]) for x in candles] if candles and len(candles[0]) >= 6 else []
                vol = [float(x[5]) for x in candles] if candles and len(candles[0]) >= 6 else []
                cmp_ratio = compression(close)

                base5 = (sum(vol[-6:-1]) / 5.0) if len(vol) >= 6 else 0.0
                vol_surge = safe_ratio(vol[-1], base5) if len(vol) >= 1 else 0.0

                ob = await tasks_o[sym]
                bids = (ob.get("data", {}) or {}).get("bids", [])[:10]
                asks = (ob.get("data", {}) or {}).get("asks", [])[:10]
                bid = sum(float(b.get("quantity", 0) or 0) for b in bids)
                ask = sum(float(a.get("quantity", 0) or 0) for a in asks)
                ob_ratio = safe_ratio(bid, ask)

                prem = None
                gate_pair = SYMBOL_MAP.get(sym)
                if usdt_krw > 0 and gate_pair and gate_pair in STATE.book:
                    mid = (STATE.book[gate_pair]["best_bid"] + STATE.book[gate_pair]["best_ask"]) / 2.0
                    prem = ((price / usdt_krw) / mid) - 1.0 if mid and mid > 0 else None

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
                    "value_24h": round(value, 0),
                    "vol_surge": round(vol_surge, 2),
                    "orderbook_ratio": round(ob_ratio, 2),
                    "premium": None if prem is None else round(prem, 4),
                    "ma_compression": round(cmp_ratio, 4),
                    "lead": round(lead, 3),
                    "score": round(score, 3),
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

            except Exception as e:
                errors.append({"symbol": sym, "error": str(e)})
                continue

        out.sort(key=lambda x: (not x["pass"], -x["score"], -x["value_24h"]))
        return jsonify({
            "ok": True,
            "candidates": out[:50],
            "lead_thresh": round(LEAD_THRESH, 2),
            "errors": errors[:10]  # 참고용으로 앞 10개만 반환
        })

# ---------- 로컬 실행 ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
