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
                    return jsonify({"ok": False, "stage": "bithumb_all", "error": "empty data"}), 200
                usdt_price = float((data.get("USDT") or {}).get("closing_price") or 0)
            except Exception as e:
                print("SCAN:bithumb_all", traceback.format_exc())
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
                    # 개별 심볼 파싱 실패는 무시
                    continue
            rows.sort(key=lambda x: x[2], reverse=True)
            cand_syms = [s for s,_,_ in rows[:TOP_N_BY_VALUE]]

            # 3) 병렬 수집
            tasks_c = {s: asyncio.create_task(candles_1h(client, s)) for s in cand_syms}
            tasks_o = {s: asyncio.create_task(orderbook(client, s)) for s in cand_syms}

            # 4) 평가
            for sym, price, value in rows[:TOP_N_BY_VALUE]:
                try:
                    candles = await tasks_c[sym]
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

                    score, lead = final_score(gate_pair or "", prem, cmp_ratio)
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
                    # 개별 심볼 실패는 기록만 하고 계속 진행
                    errors.append({"symbol": sym, "error": str(e)})
                    continue

        out.sort(key=lambda x: (not x["pass"], -x["score"], -x["value_24h"]))
        return jsonify({
            "ok": True,
            "lead_thresh": round(LEAD_THRESH, 2),
            "candidates": out[:50],
            "errors": errors[:10]  # 디버그 참고용
        }), 200

    except Exception as e:
        # 최상위 가드: 절대 500을 내지 않도록
        print("SCAN_FATAL\n", traceback.format_exc())
        return jsonify({"ok": False, "stage": "fatal", "error": str(e)}), 200
