import os

# ===== 최적 효율(단기 폭등 전용) 파라미터 =====
TOP_N_BY_VALUE = 30          # 24H 거래대금 상위만 정밀 검사
PREMIUM_MIN = 0.008          # +0.8% 이상
ORDERBOOK_IMBAL_RATIO = 1.5  # 매수벽/매도벽
VOLUME_SURGE_RATIO = 5.0     # 최근 1시간 / 직전 5시간평균 (강화)
MA_COMPRESSION_MAX = 0.05    # 1h MA20·60·120 압축 (완화: 장대양봉 직전 허용)

# 리드 신호 민감도
LEAD_THRESH_BASE = 0.9       # 조기 감지 강화를 위해 하향

# 빗썸 폴링 부스트 윈도(초) — Gate 임계치 진입 시
BOOST_WINDOW_SEC = 30        # 30초 동안 1초 폴링 유지

# 텔레그램 알림: 같은 코인 재알림 쿨다운(초)
ALERT_COOLDOWN_SEC = 120     # 단축

# 환경
BITHUMB_BASE = "https://api.bithumb.com"
GATE_REST = "https://api.gateio.ws/api/v4"
GATE_WS = "wss://api.gateio.ws/ws/v4/"

# 텔레그램
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 수동 심볼 매핑 예외(이름 불일치 보정)
MANUAL_SYMBOL_MAP = {
    # 필요 시 여기에 추가
}
