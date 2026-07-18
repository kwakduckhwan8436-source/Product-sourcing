# 위탁판매 소싱 도우미 (웹)

## 실행
run_web.bat 더블클릭 → 브라우저 http://127.0.0.1:8000

설치가 안 되면 명령창에서 직접:
    python -m pip install fastapi uvicorn httpx pydantic
    python -m uvicorn web.server:app --port 8000

## 폴더 구성 (정리됨)
    run_web.bat        실행
    web/               웹 서버 + 화면
    discovery/         판정 엔진 (네이버 조회·블루오션·진입키워드·신호등)
    _archive/          오너클랜 등 지금 안 쓰는 코드 (되살릴 수 있게 보관)

## 카페 회원과 공유
Render 등에 올릴 때 시작 명령:
    uvicorn web.server:app --host 0.0.0.0 --port $PORT
서버는 네이버 열쇠를 보관하지 않습니다. 회원 각자 자기 브라우저에 저장.
