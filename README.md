# Bithumb 급등 코인 스캐너 — 최적 효율(단기 폭등 전용)

무료 API 환경에서 유료급에 근접한 **단기 폭등 사전 포착** 스캐너.

## 설치
```bash
pip install -r requirements.txt
```

## 실행
```bash
# (선택) 텔레그램 알림
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=yyy

python app.py
```

## 엔드포인트
- `GET /health` : 상태
- `GET /symbols` : 빗썸↔Gate 교집합
- `GET /scan` : 후보 리스트(점수·리드·프리미엄·호가 불균형 등)

## 운영 권장
- 평시 호출 간격: 8초
- Gate 리드 급등 감지 시: 30초 동안 1초 간격
- 차트 확인은 상위 1~3개만
