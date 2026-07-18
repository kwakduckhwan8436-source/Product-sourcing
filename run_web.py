"""
run_web.py — 실행기

[왜 이게 필요한가]
포트 8000 을 예전 서버가 잡고 있으면 새 서버는 뜨지 못하고 죽는다
(Errno 10048). 그런데 브라우저를 열면 살아있는 '예전 서버'가 예전 화면을
보여준다 → 고친 게 하나도 안 보인다. 실제로 이 일이 있었다.

그래서 여기서는
  1) 8000 부터 비어 있는 포트를 찾아서
  2) 그 포트로 서버를 띄우고
  3) 그 주소로 브라우저를 연다
포트 충돌로 죽는 일이 없다.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def find_free_port(start: int = 8000, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("빈 포트를 찾지 못했습니다.")


def main() -> None:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("\n[설치 필요] 명령창에 아래를 붙여넣고 실행하세요:")
        print("    python -m pip install fastapi uvicorn httpx pydantic\n")
        input("엔터를 누르면 닫힙니다...")
        return

    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    if port != 8000:
        print(f"\n[알림] 8000 번은 예전 서버가 쓰고 있어 {port} 번으로 켭니다.")
        print("       예전 창은 닫으셔도 됩니다.\n")
    print(f"브라우저에서 열기 →  {url}\n")

    threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)),
                     daemon=True).start()

    import uvicorn
    uvicorn.run("web.server:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
