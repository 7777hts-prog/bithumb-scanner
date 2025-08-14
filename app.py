# app.py  — Flask 3.x 호환 / 안정가드 포함 (완성본)

import os, threading, asyncio
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


# ========== 전역 에러 핸들러: 500 방지 ==========
@app.errorhandler(Exception)
def _any_error(e):
    # 어떤 에러가 나도 JSON으로 응답하고, 서버는 계속 동작
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# ========== 유틸: 안전 연산/재시도 ==========
def safe_ratio(num: float, den: float) -> float:
    try:
        den = float(den)
        if den > 0:
            return float(num) / den
    except Exception:
        pass
    return 0.0


async def get_with_retry(client: httpx.AsyncClient, url: str, params=None, tries: int = 3):
    last_err = None
    for i in range(tries):
        try:
            r = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5 * (i + 1))  # 0.5s, 1.0s, 1.5s
    raise last_err


async def bithumb_all(client: httpx.AsyncClient):
    return await get_with_retry(client, f"{BITHUMB_BASE}/public/ticker/ALL_KRW")


async def candles_1h(client: httpx.AsyncClient, sym: str):
    data = await get_with_retry(client, f"{BITHUMB_BASE}/public/candlestick/{sym}_KRW/1h")
    if data.get("status") != "0000":
        return []
    return data.get("data", [])[-140:]  # 최근 140개만


async def orderbook(client: httpx.AsyncClient, sym: str):
    return await get_with_retry(client, f"{BITHUMB_BASE}/public/orderbook/{sym}_KRW")


# ========== 안정 초기화(Flask 3.x): 첫 요청 때 1회 ==========
_init_done = False
_init_lock = threading.Lock()


def _start_ws_thread(pairs):
    """Gate.io WS 스트림을 별도 쓰레드에서 실행 (쓰레드 내 이벤트 루프)."""
    async def _ws_main():
        await run_stream(pairs)

    t = threading.Thread(target=lambda: asyncio.run(_ws_main()), daemon=True)
    t.start()


def _blocking_init_once():
    """동기적으로 1회만 초기화: 심볼 맵 생성 + WS 시작."""
    global _init_done, SYMBOL_MAP
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        # 1) 심볼 교집합 생성 (비동기 함수를 동기 실행)
        SYMBOL_MAP = asyncio.run(build_intersection())
        # 2) WS 백그라운드 시작
        pairs = list(SYMBOL_MAP.values())
        if pairs:
            _start_ws_thread(pairs)
        _init_done = True


@app.before_request
def _ensure_initialized():
    # 모든 요청 전에 1회만 초기화
    _blocking_init_once()


# ========== 라우트 ==========
@app.get("/health")
def health():
    return {"ok": True, "ws_pairs": len(STATE.pairs), "mapped": len(SYMBOL_MAP)}


@app.get("/symbols")
def symbols():
    return {"mapped": SYMBOL_MAP}


@app.get("/scan")
async def scan():
    import traceback

    try:
        # 시장 강도에 따른 리드 임계치
        market_vps = sorted([m.get("vol_ps", 0.0) for m in STATE.metrics.values()])
        vps_med = market_vps[len(market_vps)//2] if market_vps else 0.0
        LEAD_THRESH = adaptive_lead_threshold(vps_med)

        out = []
        errors = []

        async with httpx.AsyncClient(headers={"accept": "application/json"}) as client:
            # 1) 틱커 전체
            try:
                all_t = await bithumb_all(client)
                data = all_t.get("data", {}) or {}
                if not data:
                    return jsonify({"ok": False, "stage": "bithumb_all", "error": "empty"}), 200
                usdt_price = float((data.get("USDT") or {}).get("closing_price") or 0)
            except Exception as e:
                return jsonify({"ok": False, "stage": "bithumb_all", "error": str(e)}), 200

            # 2) 거래대금 상위 선별
            rows = []
            for sym, row in data.items():
                if sym == "date":
                    continue
                try:
                    price = float((row or {}).get("closing_price") or 0)
                    value = float((row or {}).get("acc_trade_value_24H") or 0)
                    if price > 0 and value > 0:
                        rows.append((sym, price, value))
                except Exception:
                    continue

            rows.sort(key=lambda x: x[2], reverse=True)
            cand_syms = [s for s, _, _ in rows[:TOP_N_BY_VALUE]]

            # 3) 병렬 수집
            tasks_c = {s: asyncio.create_task(candles_1h(client, s)) for s in cand_syms}
            tasks_o = {s: asyncio.create_task(orderbook(client, s)) for s in cand_syms}

            # 4) 평가
            for sym, price, value in rows[:TOP_N_BY_VALUE]:
                try:
                    candles = await tasks_c[sym]
                    # 빗썸 1h 배열: [ts, open, close, high, low, vol] 형태(열 6개 보장 가드)
                    close = [float(c[2]) for c in candles if isinstance(c, (list, tuple)) and len(c) >= 6]
                    vol   = [float(c[5]) for c in candles if isinstance(c, (list, tuple)) and len(c) >= 6]

                    # MA 압축
                    cmp_ratio = compression(close) if close else 1.0

                    # 거래량 급증 (0 나눔 방지)
                    base5 = (sum(vol[-6:-1]) / 5.0) if len(vol) >= 6 else 0.0
                    lastv = vol[-1] if vol else 0.0
                    vol_surge = (lastv / base5) if base5 > 0 else 0.0

                    # 호가 불균형
                    ob = await tasks_o[sym]
                    book = (ob.get("data") or {})
                    bids = (book.get("bids") or [])[:10]
                    asks = (book.get("asks") or [])[:10]
                    bid_qty = sum(float(b.get("quantity") or 0) for b in bids if isinstance(b, dict))
                    ask_qty = sum(float(a.get("quantity") or 0) for a in asks if isinstance(a, dict))
                    ob_ratio = (bid_qty / ask_qty) if ask_qty > 0 else 0.0

                    # 프리미엄
                    prem = None
                    gate_pair = SYMBOL_MAP.get(sym)
                    if usdt_price > 0 and gate_pair and gate_pair in STATE.book:
                        mid = (STATE.book[gate_pair]["best_bid"] + STATE.book[gate_pair]["best_ask"]) / 2.0
                        prem = ((price / usdt_price) / mid) - 1.0 if (mid and mid > 0) else None

                    # 점수/리드
                    score, lead = final_score(gate_pair or "", prem, cmp_ratio)

                    # 통과 여부
                    passed = (
                        vol_surge >= VOLUME_SURGE_RATIO and
                        ob_ratio  >= ORDERBOOK_IMBAL_RATIO and
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
                        "pass": bool(passed)
                    })

                    # 알림
                    if passed and (lead >= LEAD_THRESH) and (prem is not None):
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
                    errors.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
                    continue

        out.sort(key=lambda x: (not x["pass"], -x["score"], -x["value_24h"]))
        return jsonify({
            "ok": True,
            "lead_thresh": round(LEAD_THRESH, 2),
            "candidates": out[:50],
            "errors": errors[:10]  # 참고용
        }), 200

    except Exception as e:
        # 최상위 가드: 절대 500으로 죽지 않게
        return jsonify({"ok": False, "stage": "fatal", "error": f"{type(e).__name__}: {e}"}), 200


# ========== 로컬 실행 ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
