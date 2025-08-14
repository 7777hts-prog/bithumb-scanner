import math
from typing import List
from config import MA_COMPRESSION_MAX, LEAD_THRESH_BASE
from gate_stream import STATE

def ma(series: List[float], n: int) -> float:
    return sum(series[-n:]) / n if len(series) >= n else float('nan')

def compression(close: List[float]) -> float:
    if len(close) < 120: return 1.0
    c = close[-1]
    m20, m60, m120 = ma(close,20), ma(close,60), ma(close,120)
    return abs(max(m20,m60,m120)-min(m20,m60,m120)) / c if c else 1.0

def final_score(pair_usdt: str, premium: float | None, cmp_ratio: float) -> tuple[float, float]:
    m = STATE.metrics.get(pair_usdt, {})
    ofi = abs(m.get("OFI",0.0))
    tps = m.get("trades_ps",0.0)
    vps = m.get("vol_ps",0.0)
    dba = max(1e-6, m.get("dba",1e-6))
    lead = (0.6*ofi) + (0.25*math.log1p(vps)) + (0.15*math.log1p(tps)) - (0.1*math.log1p(dba))
    prem_boost = 1.0 + max(0.0, min(premium or 0.0, 0.01))
    cmp_boost  = 1.0 + max(0.0, (MA_COMPRESSION_MAX - cmp_ratio))
    return lead * prem_boost * cmp_boost, lead

def adaptive_lead_threshold(market_vps_median: float) -> float:
    if market_vps_median <= 0.5: 
        return LEAD_THRESH_BASE + 0.2
    elif market_vps_median <= 2.0:
        return LEAD_THRESH_BASE
    elif market_vps_median <= 5.0:
        return LEAD_THRESH_BASE - 0.1
    else:
        return LEAD_THRESH_BASE - 0.2
