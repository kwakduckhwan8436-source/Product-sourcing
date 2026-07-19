"""
selftest.py — 내보내기 전에 반드시 통과해야 하는 검사.

[왜 만들었나]
'문법 에러 0' 이라고 보고했는데 사용자 PC 에서 SyntaxError 가 터졌다.
원인: ast.parse() 는 문법 트리만 만들 뿐, '같은 인자를 두 번 씀' 같은 것은
compile() 단계에서 걸린다. 즉 잘못된 자로 재고 있었다.

여기서는 세 단계로 본다.
  1) compile()  — ast.parse 가 놓치는 것까지 잡는다
  2) import     — 실제로 로드되는지 (깨진 의존성 발견)
  3) 화면 검사  — onclick 이 부르는 함수가 정의돼 있는지, 버튼에 리스너가 있는지

실행:  python selftest.py
"""
from __future__ import annotations

import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent
SKIP = ("__pycache__", "_archive")


def _mock_deps() -> None:
    h = types.ModuleType("httpx")
    h.AsyncClient = lambda *a, **k: None
    h.Response = object
    h.Client = lambda *a, **k: None
    sys.modules.setdefault("httpx", h)
    try:
        import fastapi  # noqa: F401
    except ImportError:
        fa = types.ModuleType("fastapi")

        class _App:
            def __init__(self, **k): pass
            def post(self, p): return lambda f: f
            def get(self, p): return lambda f: f
            def mount(self, *a, **k): pass

        fa.FastAPI = _App
        fa.Request = type("Request", (), {})   # 실물 fastapi.Request 대체
        fr = types.ModuleType("fastapi.responses")
        fr.FileResponse = lambda p: p
        fr.Response = lambda **k: None
        fs = types.ModuleType("fastapi.staticfiles")
        fs.StaticFiles = lambda **k: None
        pd = types.ModuleType("pydantic")

        class _BM:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        pd.BaseModel = _BM
        for k, v in (("fastapi", fa), ("fastapi.responses", fr),
                     ("fastapi.staticfiles", fs), ("pydantic", pd)):
            sys.modules[k] = v


def check_compile() -> int:
    bad = 0
    for p in sorted(ROOT.rglob("*.py")):
        if any(s in str(p) for s in SKIP):
            continue
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            bad += 1
            print(f"  [문법] {p.name}:{e.lineno} — {e.msg}")
    print(f"1) compile 검사 — 에러 {bad}개")
    return bad


def check_import() -> int:
    import importlib
    sys.path.insert(0, str(ROOT))
    _mock_deps()
    bad = 0
    mods = []
    for p in sorted((ROOT / "discovery").rglob("*.py")):
        if any(s in str(p) for s in SKIP):
            continue
        mods.append(str(p.relative_to(ROOT)).replace("/", ".")[:-3])
    mods.append("web.server")
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  [로드] {m}: {type(e).__name__}: {e}")
    print(f"2) import 검사 — {len(mods)}개 중 실패 {bad}개")
    return bad


def check_ui() -> int:
    p = ROOT / "web" / "static" / "index.html"
    if not p.exists():
        print("  [화면] index.html 없음")
        return 1
    s = p.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", s, re.S)
    if not m:
        print("  [화면] script 블록 없음")
        return 1
    js = m.group(1)
    bad = 0
    for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
        if js.count(a) != js.count(b):
            bad += 1
            print(f"  [화면] 괄호 불균형 {a}{b}")
    for fn in set(re.findall(r'onclick="([a-zA-Z_]+)\(', s)) - {"navigator"}:
        if f"function {fn}" not in js:
            bad += 1
            print(f"  [화면] onclick 이 부르는 {fn}() 정의 없음")
    for bid in ("auto", "cal", "more", "wlist", "cafe", "chk"):
        if f"$('#{bid}').addEventListener" not in js:
            bad += 1
            print(f"  [화면] #{bid} 버튼에 리스너 없음")

    # 인자 받는 함수를 리스너로 그대로 넘기면 '클릭 이벤트' 가 첫 인자로 들어간다.
    # (실제로 autoScan(exclude) 에 MouseEvent 가 들어가 서버가 422 를 뱉었다)
    sigs = {}
    for m in re.finditer(
            r"(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)", js):
        sigs[m.group(1)] = [a.strip() for a in m.group(2).split(",") if a.strip()]
    for m in re.finditer(
            r"addEventListener\(\s*'(\w+)'\s*,\s*([a-zA-Z_]\w*)\s*\)", js):
        ev, fn = m.group(1), m.group(2)
        params = sigs.get(fn) or []
        # 이벤트 객체를 일부러 받는 함수(e/ev/event)는 정상
        if params and params[0].lower() not in ("e", "ev", "event"):
            bad += 1
            print(f"  [화면] addEventListener('{ev}', {fn}) — "
                  f"{fn}({params[0]}) 의 첫 인자로 이벤트가 들어갑니다")

    # 없는 요소에 리스너를 걸면 화면 전체가 죽는다.
    # (버튼을 지우고 리스너를 안 지운 적이 있다)
    ids = set(re.findall(r'id="([a-zA-Z_]\w*)"', s))
    for m in re.finditer(r"\$\('#([a-zA-Z_]\w*)'\)\.addEventListener", js):
        if m.group(1) not in ids:
            bad += 1
            print(f"  [화면] #{m.group(1)} 요소가 없는데 리스너를 검 — 화면이 죽습니다")

    # 화면에 반드시 있어야 할 것들.
    # (상단바를 갈아끼우다 위탁/도매 토글을 통째로 잘라먹은 적이 있다 —
    #  CSS·JS 는 남아 있어서 문법 검사로는 안 잡혔다)
    must_have = {
        "위탁/도매 토글": 'data-mode="consign"',
        "도매 버튼": 'data-mode="wholesale"',
        "카페 링크": "cafe.naver.com/aiprogram1",
        "브랜드 로고": "재테크 연구소",
        "분야 선택": 'id="cat"',
    }
    for label, token in must_have.items():
        if token not in s:
            bad += 1
            print(f"  [화면] {label} 이(가) 사라졌습니다")

    # 아무도 안 부르는 함수 = 죽은 코드.
    # (같은 기능을 두 번 만들어 버튼이 중복된 적이 있다 — titleAB / titleCheck)
    called = set(re.findall(r'onclick="([a-zA-Z_]\w*)\(', s))
    called |= set(re.findall(r"addEventListener\([^,]+,\s*([a-zA-Z_]\w*)\s*\)", js))
    for m in re.finditer(r"(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\(", js):
        fn = m.group(1)
        # 다른 곳에서 fn( 형태로 불리는지
        uses = len(re.findall(r"\b" + re.escape(fn) + r"\s*\(", js))
        if fn not in called and uses <= 1:
            bad += 1
            print(f"  [화면] {fn}() 를 아무도 안 부릅니다 — 죽은 코드")

    print(f"3) 화면 검사 — 문제 {bad}개")
    return bad

def main() -> int:
    print("=== selftest ===")
    total = check_compile() + check_import() + check_ui()
    print("=" * 30)
    if total:
        print(f"실패 {total}건 — 내보내면 안 됩니다")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
