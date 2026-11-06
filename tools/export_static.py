"""대시보드를 정적 HTML로 내보냅니다 (GitHub Pages 게시용).

    python tools/export_static.py            # → docs/
    python tools/export_static.py out_dir

웹 대시보드(taxrefund/web.py)의 렌더링 함수를 그대로 쓰고, 링크만 정적
파일 경로로 바꿉니다. 서버가 하던 '전체' 화면의 필터·검색은 브라우저
스크립트로 대신합니다. 공인 IP·Tailscale 주소 같은 운영 정보와 접근
토큰은 넣지 않습니다.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from taxrefund.config import load_config  # noqa: E402
from taxrefund.web import (  # noqa: E402
    TAG_KINDS,
    _e,
    _page,
    alert_by_key,
    gather,
    render_all,
    render_browse,
    render_detail,
    render_home,
)

KST = timezone(timedelta(hours=9))


def detail_file(key: str) -> str:
    # "prec:621195" 처럼 콜론이 들어가면 상대 링크가 URL 스킴으로 읽힙니다.
    return "d-" + re.sub(r"[^0-9A-Za-z_.-]", "-", key) + ".html"


LAW_API = re.compile(r"https?://www\.law\.go\.kr/DRF/lawService\.do\?[^\s\"'<\\]*")


def public_law_url(m: re.Match) -> str:
    """OPEN API 주소 → 국가법령정보센터 열람 주소.

    API 주소에는 발급받은 OC 값이 들어 있어 공개 페이지에 둘 수 없습니다.
    """
    query = html.unescape(m.group(0)).split("?", 1)[1]
    q = dict(p.partition("=")[::2] for p in query.split("&"))
    seq = q.get("ID", "")
    if q.get("target") == "expc":
        return f"https://www.law.go.kr/LSW/expcInfoP.do?expcSeq={seq}"
    return f"https://www.law.go.kr/LSW/precInfoP.do?precSeq={seq}"


def rewrite_links(page: str) -> str:
    """서버 경로(/, /all, /d/<key> ...)를 정적 파일 경로로."""
    page = LAW_API.sub(public_law_url, page)

    def fix(url: str) -> str:
        url = html.unescape(url)
        if url.startswith(("http://", "https://", "#", "mailto:")):
            return url
        path, sep, query = url.partition("?")
        path = path.rstrip("/") or "/"
        if path == "/":
            target = "index.html"
        elif path == "/all":
            target = "all.html"
        elif path == "/browse":
            target = "browse.html"
        elif path == "/status":
            target = "status.html"
        elif path.startswith("/d/"):
            target = detail_file(path[3:])
            sep = query = ""
        else:
            return url
        return target + (sep + query if query else "")

    def sub(m: re.Match) -> str:
        return f'{m.group(1)}="{html.escape(fix(m.group(2)), quote=True)}"'

    return re.sub(r'\b(href|action)="([^"]*)"', sub, page)


def stamp(page: str, generated: str) -> str:
    note = (
        '<div style="font-size:12px;color:var(--sub);margin:0 0 12px">'
        f"정적 스냅샷 · {generated} 기준</div>"
    )
    return page.replace("<main>", "<main>" + note, 1)


ALL_JS = r"""
(function(){
  var KINDS = %(kinds)s;
  var params = new URLSearchParams(location.search);
  var mode = params.get('f') || 'all';
  var text = (params.get('q') || '').trim();
  var picked = {};
  KINDS.forEach(function(k){ picked[k] = params.getAll(k).filter(function(v){return v.trim();}); });

  function qs(m, pk, t, drop, add){
    var p = new URLSearchParams();
    if (m && m !== 'all') p.append('f', m);
    KINDS.forEach(function(k){ (pk[k]||[]).forEach(function(v){
      if (drop && drop[0]===k && drop[1]===v) return; p.append(k, v); }); });
    if (add && !(drop && drop[0]===add[0] && drop[1]===add[1])) p.append(add[0], add[1]);
    if (t) p.append('q', t);
    var s = p.toString(); return 'all.html' + (s ? '?' + s : '');
  }

  var cards = Array.prototype.slice.call(document.querySelectorAll('.card[data-row]'));
  var shown = 0;
  var words = text.toLowerCase().split(/\s+/).filter(Boolean);
  cards.forEach(function(c){
    var r = JSON.parse(c.getAttribute('data-row'));
    var ok = true;
    if (mode === 'alert') ok = r.passed;
    else if (mode === 'pending') ok = r.pending;
    else if (mode === 'rejected') ok = !r.passed;
    KINDS.forEach(function(k){
      if (!ok || !picked[k].length) return;
      var have = r.tags[k] || [];
      ok = picked[k].some(function(v){ return have.indexOf(v) >= 0; });
    });
    if (ok) ok = words.every(function(w){ return r.hay.indexOf(w) >= 0; });
    c.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });

  // 상태 칩
  document.querySelectorAll('.chips a.chip').forEach(function(a){
    var m = new URLSearchParams(a.getAttribute('href').split('?')[1] || '').get('f') || 'all';
    a.classList.toggle('on', m === mode);
    a.setAttribute('href', qs(m, picked, text));
  });
  // 태그 칩: 켜고 끄기
  document.querySelectorAll('a.tchip').forEach(function(a){
    var p = new URLSearchParams(a.getAttribute('href').split('?')[1] || '');
    var kv = null;
    KINDS.forEach(function(k){ if (p.get(k)) kv = [k, p.get(k)]; });
    if (!kv) return;
    var on = picked[kv[0]].indexOf(kv[1]) >= 0;
    a.classList.toggle('on', on);
    a.setAttribute('href', on ? qs(mode, picked, text, kv) : qs(mode, picked, text, null, kv));
  });
  // 검색창
  var form = document.querySelector('form.search');
  if (form) {
    form.querySelectorAll('input[type=hidden]').forEach(function(i){ i.remove(); });
    function hid(n, v){ var i=document.createElement('input'); i.type='hidden'; i.name=n; i.value=v; form.insertBefore(i, form.firstChild); }
    if (mode !== 'all') hid('f', mode);
    KINDS.forEach(function(k){ picked[k].forEach(function(v){ hid(k, v); }); });
    form.querySelector('input[name=q]').value = text;
  }
  // 건수 줄
  var nTags = KINDS.reduce(function(s,k){ return s + picked[k].length; }, 0);
  var fsum = document.querySelector('.fsum');
  if (fsum) {
    fsum.innerHTML = shown + '건' + (text ? ' · 검색 ' + text.replace(/[<>&"]/g,'') : '')
      + (nTags ? ' · 태그 ' + nTags + '개' : '')
      + ((nTags || text) ? '<a class="clear" href="all.html">필터 해제</a>' : '');
  }
  var empty = document.getElementById('static-empty');
  if (empty) empty.style.display = shown ? 'none' : '';
})();
"""


def build_all(data: dict) -> str:
    page = render_all(data, None).decode("utf-8")
    rows = data["rows"]
    it = iter(rows)

    def attach(_m: re.Match) -> str:
        r = next(it)
        payload = {
            "passed": bool(r["passed"]),
            "pending": bool(r["pending"]),
            "tags": {k: list(v) for k, v in r["tags"].items()},
            "hay": r["haystack"],
        }
        return f'<div class="card" data-row="{html.escape(json.dumps(payload, ensure_ascii=False), quote=True)}">'

    page = re.sub(r'<div class="card">', attach, page, count=len(rows))
    script = ALL_JS % {"kinds": json.dumps(list(TAG_KINDS))}
    empty = ('<div class="empty" id="static-empty" style="display:none">'
             "조건에 맞는 판례가 없습니다. 태그를 줄이거나 검색어를 바꿔 보세요.</div>")
    page = page.replace("</main>", empty + "</main>", 1)
    return page.replace("</body>", f"<script>{script}</script></body>", 1)


def build_status(data: dict) -> str:
    cfg = data["cfg"]
    c = data["counts"]
    watch = "".join(
        f"<li><b>{_e(w.name)}</b> — {_e(w.industry or '-')}</li>" for w in cfg.watchlist
    ) or "<li>워치리스트 비어 있음</li>"
    body = f"""<section><h3>수집 현황</h3><dl class="kv">
<dt>수집 판례</dt><dd>{c['precedents']}건</dd>
<dt>분석 완료</dt><dd>{c['analyses']}건</dd>
<dt>알림 대상</dt><dd>{len(data['alerts'])}건</dd>
<dt>마지막 수집</dt><dd>{_e(data['last_run'] or '없음')}</dd>
<dt>최소 점수</dt><dd>{cfg.notify.min_confidence}점</dd>
</dl></section>
<section><h3>워치리스트 (업종군)</h3><ul>{watch}</ul></section>"""
    return _page("상태", body, "status", None).decode("utf-8")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs"
    cfg = load_config()
    cfg.secrets.web_token = None
    data = gather(cfg)
    generated = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    pages = {
        "index.html": render_home(data, None).decode("utf-8"),
        "browse.html": render_browse(data, None).decode("utf-8"),
        "status.html": build_status(data),
        "all.html": build_all(data),
    }
    for r in data["rows"]:
        key = r["prec"].key
        alert = alert_by_key(cfg, key)
        if alert is not None:
            pages[detail_file(key)] = render_detail(alert, None).decode("utf-8")

    for name, page in pages.items():
        (out / name).write_text(stamp(rewrite_links(page), generated), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"{len(pages)} pages → {out}")


if __name__ == "__main__":
    main()
