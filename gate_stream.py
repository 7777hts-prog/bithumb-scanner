import asyncio, json, time
from collections import deque, defaultdict
import websockets
from config import GATE_WS

class GateState:
    def __init__(self):
        self.book = {}
        self.metrics = {}
        self.trades_q = defaultdict(lambda: deque(maxlen=200))
        self.pairs = []

STATE = GateState()

def _now(): return time.time()

def ofi_delta(bids, asks, prev):
    best_bid = float(bids[0][0]) if bids else prev.get("best_bid", 0.0)
    best_ask = float(asks[0][0]) if asks else prev.get("best_ask", 0.0)
    ofi = (best_bid - prev.get("best_bid", best_bid)) - (best_ask - prev.get("best_ask", best_ask))
    return ofi, best_bid, best_ask

async def _consumer():
    subs = []
    for p in STATE.pairs:
        subs += [
            {"channel":"spot.order_book_update","event":"subscribe","payload":[p, "100ms"]},
            {"channel":"spot.trades","event":"subscribe","payload":[p]}
        ]
    async with websockets.connect(GATE_WS, ping_interval=20, ping_timeout=10) as ws:
        for s in subs:
            await ws.send(json.dumps(s))
        while True:
            msg = json.loads(await ws.recv())
            ch = msg.get("channel"); ev = msg.get("event")
            if ch == "spot.order_book_update" and ev in ("update","all"):
                res = msg.get("result", {})
                p = res.get("s")
                bids = res.get("b", []); asks = res.get("a", [])
                prev = STATE.book.get(p, {})
                ofi, bb, ba = ofi_delta(bids, asks, prev)
                STATE.book[p] = {"best_bid": bb, "best_ask": ba, "ts": _now()}
                dba = (ba - bb) / ba if ba else 0.0
                m = STATE.metrics.get(p, {"OFI":0.0,"trades_ps":0.0,"vol_ps":0.0,"dba":0.0})
                m["OFI"] = 0.8*m["OFI"] + 0.2*ofi
                m["dba"] = 0.8*m["dba"] + 0.2*dba
                STATE.metrics[p] = m
            elif ch == "spot.trades" and ev == "update":
                for t in msg.get("result", []):
                    p = t["s"]; sz = float(t["q"]); ts = _now()
                    STATE.trades_q[p].append((ts, sz))
                for p, dq in list(STATE.trades_q.items()):
                    cutoff = _now() - 1.0
                    while dq and dq[0][0] < cutoff: dq.popleft()
                    m = STATE.metrics.get(p, {"OFI":0.0,"trades_ps":0.0,"vol_ps":0.0,"dba":0.0})
                    m["trades_ps"] = float(len(dq))
                    m["vol_ps"] = float(sum(x[1] for x in dq))
                    STATE.metrics[p] = m

async def run_stream(pairs):
    STATE.pairs = list(pairs)
    while True:
        try:
            await _consumer()
        except Exception:
            await asyncio.sleep(3)
