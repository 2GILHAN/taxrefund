"""수집·분석 결과를 폰에서 볼 수 있는 웹 대시보드.

의존성을 늘리지 않으려고 표준 라이브러리 http.server만 씁니다.
같은 와이파이면 폰 브라우저에서 http://<PC의 LAN IP>:8787 로 접속합니다.
"""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import subprocess
import threading
from pathlib import Path
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .config import Config
from .matcher import build_alerts, compute_deadlines, match_companies, tax_type_allowed
from .models import Alert, Analysis, Precedent, display_title
from .store import META_LAST_IP, META_LAST_RUN, META_LAW_OK_IP, Store

log = logging.getLogger(__name__)

# Tailscale CLI. 경로 리터럴 대신 환경변수를 써서 설치 위치 차이를 흡수합니다.
TAILSCALE_EXE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale" / "tailscale.exe"

TOKEN_COOKIE = "tr_token"


# ------------------------------------------------------------------ 데이터

def _reject_reason(prec: Precedent, analysis: Analysis, cfg: Config) -> str:
    """알림 기준을 왜 통과하지 못했는지. 통과했으면 빈 문자열."""
    if not tax_type_allowed(prec, cfg.notify):
        return f"알림 세목 아님 ({prec.resolved_tax_type or '세목 미상'})"
    if not analysis.refund_claim_possible:
        return "경정청구 실익 없음"
    if analysis.refund_confidence < cfg.notify.min_confidence:
        return f"점수 미달 ({analysis.refund_confidence} < {cfg.notify.min_confidence})"
    if cfg.notify.require_generalizable and not analysis.generalizable:
        return "일반화 어려움 (개별 사실관계)"
    if not match_companies(prec, analysis, cfg.watchlist):
        return "워치리스트 매칭 없음"
    return ""


# 태그 종류와 화면에 쓰는 이름. 순서가 곧 화면 순서입니다.
TAG_KINDS: dict[str, str] = {
    "tax": "세목",
    "industry": "업종",
    "issue": "쟁점",
    "outcome": "결과",
}

# 한 종류에서 칩으로 보여 줄 최대 개수. 쟁점 태그는 종류가 많아서
# 전부 깔면 폰 화면이 태그로만 채워집니다.
TAG_FACET_LIMIT = 24

# 분류 화면에서 한 축에 보여 줄 최대 개수.
BROWSE_LIMIT = 40


def _haystack(prec: Precedent, analysis: Analysis, tags: dict[str, list[str]]) -> str:
    """검색 대상 문자열. 소문자로 맞춰 둡니다."""
    parts = [
        display_title(prec, analysis),
        prec.title,
        prec.case_no,
        prec.court,
        prec.decided_str,
        analysis.issue_summary,
        analysis.refund_rationale,
        analysis.generalizable_reason,
    ]
    for values in tags.values():
        parts.extend(values)
    return " ".join(p for p in parts if p).lower()


def gather(cfg: Config) -> dict[str, Any]:
    """대시보드가 쓸 데이터를 한 번에 모읍니다."""
    with Store(cfg.db_path) as store:
        pairs = store.analyzed_pairs()  # 중복 표시를 위해 전부 읽습니다
        counts = store.counts()
        last_run = store.get_meta(META_LAST_RUN)
        public_ip = store.get_meta(META_LAST_IP)
        law_ok_ip = store.get_meta(META_LAW_OK_IP)

        # 같은 판결이 두 출처에서 각각 들어와 있으면 목록에 두 번 뜨고
        # 알림도 두 번 갑니다. 대표 한 건만 남깁니다.
        dup_secondary = store.duplicate_secondaries()
        alerts = build_alerts(
            [(p, a) for p, a in pairs if p.key not in dup_secondary],
            cfg.watchlist, cfg.notify,
        )
        alert_keys = {a.precedent.key for a in alerts}

        pending: dict[str, int] = {}
        pending_keys: set[str] = set()
        for channel in cfg.notify.channels:
            unsent = [a for a in alerts if not store.was_notified(a.precedent.key, channel)]
            pending[channel] = len(unsent)
            pending_keys.update(a.precedent.key for a in unsent)

        tags = store.all_tags()
        facets = {kind: store.tag_counts(kind, limit=TAG_FACET_LIMIT) for kind in TAG_KINDS}

    rows = []
    for prec, analysis in pairs:
        rows.append(
            {
                "prec": prec,
                "analysis": analysis,
                "passed": prec.key in alert_keys,
                "pending": prec.key in pending_keys,
                "reason": _reject_reason(prec, analysis, cfg),
                # 같은 판결의 다른 출처본. 목록에는 남기되 표시해 둡니다.
                "duplicate": prec.key in dup_secondary,
                "tags": tags.get(prec.key, {}),
                # 검색은 이 문자열 하나만 봅니다. 화면에 보이는 것(제목·요지·
                # 태그)은 검색되고, 안 보이는 것은 검색되지 않아야 사람이
                # 결과를 납득합니다.
                "haystack": _haystack(prec, analysis, tags.get(prec.key, {})),
            }
        )
    rows.sort(key=lambda r: (r["passed"], r["analysis"].refund_confidence), reverse=True)

    return {
        "alerts": alerts,
        "rows": rows,
        "counts": counts,
        "last_run": last_run,
        "public_ip": public_ip,
        "law_ok_ip": law_ok_ip,
        "pending": pending,
        "pending_keys": pending_keys,
        "facets": facets,
        # 알림 화면의 바로가기 줄이 씁니다. 판례 키 -> 종류별 태그.
        "tags": tags,
        "cfg": cfg,
    }


def alert_by_key(cfg: Config, key: str) -> Alert | None:
    with Store(cfg.db_path) as store:
        for prec, analysis in store.analyzed_pairs():
            if prec.key == key:
                return Alert(
                    precedent=prec,
                    analysis=analysis,
                    matches=match_companies(prec, analysis, cfg.watchlist),
                    deadlines=compute_deadlines(analysis, prec),
                )
    return None


# ------------------------------------------------------------------ 렌더링

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--txt:#1c1e21;--sub:#6b7280;--line:#e5e7eb;
      --hi:#e74c3c;--mid:#e67e22;--lo:#3498db;--ok:#16a34a;--warn:#f59e0b}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--card:#1e2126;--txt:#e8eaed;
      --sub:#9aa0a6;--line:#2c3036}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);
     font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",
     "Segoe UI",Roboto,sans-serif;font-size:16px;line-height:1.6;
     padding-bottom:env(safe-area-inset-bottom)}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:10;background:var(--card);border-bottom:1px solid var(--line);
       padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:650}
nav{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
nav a{font-size:13px;padding:6px 12px;border-radius:999px;background:var(--bg);
      border:1px solid var(--line);white-space:nowrap}
nav a.on{background:var(--txt);color:var(--card);border-color:var(--txt)}
main{padding:16px;max-width:820px;margin:0 auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:8px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;
      text-align:center;display:block;transition:transform .06s,border-color .12s}
.stat:active{transform:scale(.96);border-color:var(--txt)}
@media(hover:hover){.stat:hover{border-color:var(--txt)}}
.stat b{display:block;font-size:22px;line-height:1.2}
.stat span{font-size:11px;color:var(--sub)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.chip{font-size:13px;padding:7px 14px;border-radius:999px;background:var(--card);
      border:1px solid var(--line);color:var(--sub);white-space:nowrap}
.chip.on{background:var(--txt);color:var(--card);border-color:var(--txt);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
      padding:14px;margin-bottom:12px;display:block}
.card h2{font-size:16px;margin:0 0 8px;line-height:1.45;font-weight:640}
.meta{font-size:12px;color:var(--sub);margin-bottom:8px}
.sum{font-size:14px;color:var(--txt);opacity:.9}
.score{display:inline-block;min-width:44px;text-align:center;color:#fff;font-weight:700;
       font-size:13px;padding:3px 8px;border-radius:8px;margin-right:8px}
.s-hi{background:var(--hi)}.s-mid{background:var(--mid)}.s-lo{background:var(--lo)}
.s-no{background:var(--sub)}
.tags{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;padding:3px 9px;border-radius:999px;background:var(--bg);
     border:1px solid var(--line);color:var(--sub)}
.tag.co{border-color:var(--ok);color:var(--ok)}
.tag.no{border-color:var(--sub)}
.tag.smp{border-color:var(--warn);color:var(--warn);font-weight:600}
.tag.tx{border-color:var(--lo);color:var(--lo)}
.tag.ind{border-color:var(--line);color:var(--txt);opacity:.75}
.tag.src{border-color:var(--line);color:var(--sub);opacity:.7}
.search{display:flex;gap:6px;margin-bottom:12px}
.search input[type=search]{flex:1;min-width:0;font-size:15px;padding:9px 13px;
     border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--txt);
     -webkit-appearance:none}
.search button{font-size:14px;padding:9px 16px;border-radius:10px;border:1px solid var(--txt);
     background:var(--txt);color:var(--card);font-weight:600;white-space:nowrap}
.facet{display:flex;gap:8px;align-items:flex-start;margin-bottom:8px}
.fname{font-size:11px;color:var(--sub);flex:0 0 34px;padding-top:6px;font-weight:600}
/* 태그가 많아 줄바꿈으로 쌓이면 카드가 화면 밖으로 밀립니다.
   가로 스크롤로 두어 목록이 늘 보이게 합니다. */
.tchips{display:flex;gap:5px;overflow-x:auto;padding-bottom:3px;
     scrollbar-width:none;-ms-overflow-style:none}
.tchips::-webkit-scrollbar{display:none}
.tchip{font-size:12px;padding:5px 11px;border-radius:999px;background:var(--card);
     border:1px solid var(--line);color:var(--sub);white-space:nowrap;flex:0 0 auto}
.tchip.on{background:var(--lo);color:#fff;border-color:var(--lo);font-weight:600}
.tchip i{font-style:normal;opacity:.55;margin-left:5px;font-size:11px}
.fsum{font-size:12px;color:var(--sub);margin:10px 0 12px}
.clear{margin-left:10px;color:var(--hi);border-bottom:1px solid currentColor}
/* 카드는 더 이상 통째로 링크가 아닙니다. 안에 태그 링크가 들어가는데
   링크 안의 링크는 브라우저가 끊어 버리기 때문입니다. 제목만 링크입니다. */
.card .ttl{display:block}
.card .tags:empty{display:none}
.card .tags{margin-top:8px}
.tag.iss{border-color:var(--line);color:var(--sub)}
.who{margin-top:8px;font-size:11px;color:var(--sub);opacity:.85}
.quick{background:var(--card);border:1px solid var(--line);border-radius:14px;
     padding:12px 14px;margin-bottom:14px}
.quick .facet:last-child{margin-bottom:0}
.lead{font-size:14px;color:var(--sub);margin-bottom:14px;line-height:1.65}
.blist{display:flex;flex-direction:column;gap:1px;background:var(--line);
     border-radius:10px;overflow:hidden}
.brow{display:flex;align-items:center;gap:10px;background:var(--card);
     padding:11px 13px}
.brow:active{background:var(--bg)}
@media(hover:hover){.brow:hover{background:var(--bg)}}
.bname{flex:1;font-size:14px;min-width:0;overflow:hidden;text-overflow:ellipsis;
     white-space:nowrap}
.bnum{font-size:12px;color:var(--sub);white-space:nowrap}
.bmore{font-size:12px;color:var(--sub);margin-top:8px}
.banner{border-radius:12px;padding:12px 14px;margin-bottom:14px;font-size:14px;
        border:1px solid var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent)}
.banner b{color:var(--warn)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:14px;margin-bottom:12px}
section h3{font-size:13px;margin:0 0 10px;color:var(--sub);font-weight:600;
           letter-spacing:.04em;text-transform:uppercase}
ul{margin:0;padding-left:20px}li{margin-bottom:6px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:14px}
.kv dt{color:var(--sub)}.kv dd{margin:0}
.empty{text-align:center;color:var(--sub);padding:48px 16px;font-size:14px}
footer{text-align:center;color:var(--sub);font-size:11px;padding:24px 16px}
.back{font-size:13px;color:var(--sub);margin-bottom:12px;display:inline-block}
code{background:var(--bg);padding:2px 6px;border-radius:6px;font-size:13px}
.actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;
     margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
button.copy{font:inherit;font-size:14px;padding:9px 14px;border-radius:10px;cursor:pointer;
     background:var(--txt);color:var(--card);border:1px solid var(--txt);
     transition:transform .06s,background .12s,color .12s,border-color .12s}
button.copy:active{transform:scale(.96)}
button.copy.ghost{background:var(--card);color:var(--txt)}
button.copy.mini{font-size:11px;padding:3px 9px;border-radius:999px;margin-left:8px;
     background:var(--bg);color:var(--sub);border-color:var(--line);
     font-weight:500;letter-spacing:0;text-transform:none;vertical-align:middle}
button.copy.done{background:var(--ok);color:#fff;border-color:var(--ok)}
button.copy.fail{background:var(--warn);color:#fff;border-color:var(--warn)}
.urltext{word-break:break-all;font-size:13px;color:var(--lo)}
#tr-manual{position:sticky;bottom:0;margin-top:14px;background:var(--card);
     border:2px solid var(--warn);border-radius:12px;padding:12px;
     box-shadow:0 -4px 20px rgba(0,0,0,.18)}
.manual-head{font-size:14px;color:var(--txt);margin-bottom:8px;display:flex;
     align-items:center;gap:8px;flex-wrap:wrap}
.manual-head b{color:var(--warn)}
#tr-manual-all,#tr-manual-close{font:inherit;font-size:12px;padding:5px 11px;
     border-radius:8px;background:var(--bg);color:var(--txt);
     border:1px solid var(--line);cursor:pointer}
#tr-manual-all{margin-left:auto}
#tr-manual textarea{width:100%;font-size:16px;line-height:1.5;padding:10px;
     border-radius:8px;border:1px solid var(--line);background:var(--bg);
     color:var(--txt);resize:vertical;font-family:inherit;-webkit-user-select:text}
.manual-tip{font-size:11px;color:var(--sub);margin-top:8px;line-height:1.5}
"""


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _copy_data(elem_id: str, text: str) -> str:
    """복사할 원문을 JSON으로 심어 둡니다.

    HTML 속성에 넣으면 줄바꿈·따옴표 처리가 지저분해지므로 script 블록에
    담습니다. '<'를 이스케이프해 문서 안에서 태그로 해석될 여지를 없앱니다.
    """
    payload = json.dumps(text, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" id="{elem_id}">{payload}</script>'


def _copy_button(target_id: str, label: str, cls: str = "") -> str:
    return (
        f'<button type="button" class="copy {cls}" data-target="{target_id}" '
        f'data-label="{_e(label)}">{_e(label)}</button>'
    )


def detail_copy_text(alert: Alert) -> str:
    """상단 정보 박스 + 쟁점 + 실익 + 원문 링크.

    버튼을 여러 개 두는 대신 한 번에 쓸 만한 분량만 담습니다.
    (확인 체크리스트·청구 기한은 화면에서 보는 용도라 넣지 않습니다.)
    """
    p, a = alert.precedent, alert.analysis
    kind = "판례" if p.source == "prec" else "법령해석례"

    lines = [
        f"[{alert.score}점] {alert.title}",
        "",
        f"사건명: {p.title}",
        f"구분: {kind} · {p.court or '-'}",
        f"사건번호: {p.case_no or '-'}",
        f"선고일: {p.decided_str}",
        f"결론: {a.outcome}",
        f"청구 근거: {a.claim_basis}",
        f"영향도: {a.estimated_impact}",
        f"적용 업종: {', '.join(a.applicable_industries) or '-'}",
        "",
        "■ 쟁점 요약",
        a.issue_summary or "-",
        "",
        "■ 경정청구 실익",
        a.refund_rationale or "-",
    ]

    if p.url:
        lines += ["", f"원문: {p.url}"]

    return "\n".join(lines)


def _sample_badge(prec: Precedent) -> str:
    return '<span class="tag smp">샘플</span>' if prec.is_sample else ""


def _score_class(score: int, passed: bool = True) -> str:
    if not passed:
        return "s-no"
    if score >= 80:
        return "s-hi"
    if score >= 70:
        return "s-mid"
    return "s-lo"


# 클립보드 처리에는 두 가지 제약이 있습니다.
#
# 1) navigator.clipboard는 보안 컨텍스트(HTTPS 또는 localhost)에서만 동작합니다.
#    이 대시보드는 Tailscale IP로 HTTP를 쓰므로 폰에서는 쓸 수 없습니다.
# 2) 대체 수단인 document.execCommand('copy')는 iOS Safari에서 **true를 반환하면서도
#    실제로는 복사되지 않습니다.** 반환값을 믿으면 "복사됨"이라고 거짓 안내를 하게 됩니다.
#
# 그래서 iOS + 비보안 컨텍스트에서는 execCommand를 아예 시도하지 않고,
# 바로 선택 가능한 텍스트를 펼쳐 사용자가 직접 복사하도록 합니다.
COPY_JS = """
function trIsIOS(){
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function trCopy(text){
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  // iOS에서는 execCommand가 성공을 반환해도 클립보드에 들어가지 않습니다.
  // 거짓 성공을 표시하지 않도록 시도조차 하지 않습니다.
  if (trIsIOS()) {
    return Promise.reject(new Error('ios-insecure'));
  }
  return new Promise(function(resolve, reject){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    try {
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand failed'));
    } catch (err) {
      document.body.removeChild(ta);
      reject(err);
    }
  });
}

function trFlash(btn, msg, bad){
  var original = btn.dataset.label;
  btn.textContent = msg;
  btn.classList.add(bad ? 'fail' : 'done');
  setTimeout(function(){
    btn.textContent = original;
    btn.classList.remove('done', 'fail');
  }, 1800);
}

// 자동 복사가 불가능한 경우: 내용을 펼쳐 전체 선택해 둡니다.
// 사용자는 길게 눌러 '복사'만 누르면 됩니다.
function trManual(text){
  var box = document.getElementById('tr-manual');
  if (!box) return;
  var ta = box.querySelector('textarea');
  ta.value = text;
  box.style.display = 'block';
  box.scrollIntoView({behavior:'smooth', block:'center'});
  setTimeout(function(){
    ta.focus();
    ta.setSelectionRange(0, ta.value.length);
  }, 250);
}

document.addEventListener('click', function(e){
  var btn = e.target.closest('.copy');
  if (btn) {
    e.preventDefault();
    e.stopPropagation();
    var el = document.getElementById(btn.dataset.target);
    if (!el) return;
    var text = JSON.parse(el.textContent);
    trCopy(text).then(function(){
      trFlash(btn, '\\u2705 복사됨');
    }).catch(function(){
      trFlash(btn, '\\u2193 아래에서 복사', true);
      trManual(text);
    });
    return;
  }
  if (e.target.id === 'tr-manual-close') {
    document.getElementById('tr-manual').style.display = 'none';
  }
  if (e.target.id === 'tr-manual-all') {
    var ta2 = document.querySelector('#tr-manual textarea');
    ta2.focus();
    ta2.setSelectionRange(0, ta2.value.length);
  }
});
"""

MANUAL_BOX = """
<div id="tr-manual" style="display:none">
  <div class="manual-head">
    <b>길게 눌러 → 복사</b>
    <button type="button" id="tr-manual-all">전체선택</button>
    <button type="button" id="tr-manual-close">닫기</button>
  </div>
  <textarea readonly rows="12"></textarea>
  <div class="manual-tip">이 화면은 HTTPS가 아니라 브라우저가 자동 복사를 막습니다.
  한 번만 설정하면 버튼 한 번으로 복사됩니다 — 상태 화면의 안내를 참고하세요.</div>
</div>
"""


def _page(title: str, body: str, active: str, token: str | None) -> bytes:
    q = f"?t={_e(token)}" if token else ""
    nav = "".join(
        f'<a href="{path}{q}" class="{"on" if key == active else ""}">{label}</a>'
        for key, path, label in (
            ("home", "/", "알림"),
            ("all", "/all", "전체"),
            ("browse", "/browse", "분류"),
            ("status", "/status", "상태"),
        )
    )
    return f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1e2126">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{_e(title)}</title><style>{CSS}</style></head><body>
<header><h1>📑 법인세 경정청구</h1><nav>{nav}</nav></header>
<main>{body}{MANUAL_BOX}</main>
<footer>자동 분석 결과입니다. 실제 경정청구 전 세무대리인 검토가 필요합니다.</footer>
<script>{COPY_JS}</script>
</body></html>""".encode("utf-8")


def _axis_chips(
    prec: Precedent,
    analysis: Analysis,
    token: str | None,
    *,
    industries: int = 2,
    issues: int = 3,
) -> str:
    """세목 · 업종 · 주제를 누를 수 있는 칩으로.

    카드 안에서 쓰이므로 카드 자체는 <a>가 될 수 없습니다(중첩 링크는
    브라우저가 제멋대로 끊습니다). 제목만 링크로 두고 칩은 각자 링크입니다.
    """
    def chip(kind: str, value: str, cls: str) -> str:
        href = _query_string(token, "all", {}, "", add=(kind, value))
        return f'<a class="tag {cls}" href="/all{href}">{_e(value)}</a>'

    out = ""
    tax = prec.resolved_tax_type
    if tax:
        out += chip("tax", tax, "tx")
    for value in analysis.applicable_industries[:industries]:
        value = str(value).strip()
        if value:
            out += chip("industry", value, "ind")
    for value in analysis.issue_tags[:issues]:
        value = str(value).strip()
        if value:
            out += chip("issue", value, "iss")
    return out


def _card(
    prec: Precedent,
    analysis: Analysis,
    token: str | None,
    *,
    score: int,
    passed: bool = True,
    badges: str = "",
    summary: bool = True,
    companies: list[str] | None = None,
) -> str:
    """목록에 쓰는 카드 하나. 알림 화면과 전체 화면이 같은 모양을 씁니다."""
    q = f"?t={_e(token)}" if token else ""
    body = ""
    if summary and analysis.issue_summary:
        text = analysis.issue_summary
        body = (
            f'<div class="sum">{_e(text[:180])}{"…" if len(text) > 180 else ""}</div>'
        )
    matched = ""
    if companies:
        matched = (
            f'<div class="who">해당 업종군: {_e(", ".join(companies[:4]))}</div>'
        )
    return f"""<div class="card">
<a class="ttl" href="/d/{_e(prec.key)}{q}">
<h2><span class="score {_score_class(score, passed)}">{score}</span>{_e(display_title(prec, analysis))}</h2>
<div class="meta">{_e(prec.title)} · {_e(prec.court or '-')} · {_e(prec.case_no or '-')}
 · {_e(prec.decided_str)} · {_e(analysis.outcome)} · 영향도 {_e(analysis.estimated_impact)}</div>
{body}</a>
<div class="tags">{_axis_chips(prec, analysis, token)}</div>
<div class="tags">{_sample_badge(prec)}{badges}</div>
{matched}</div>"""


def render_home(data: dict[str, Any], token: str | None) -> bytes:
    alerts: list[Alert] = data["alerts"]
    c = data["counts"]
    # 채널별 건수를 더하면 한 판례가 채널 수만큼 곱해집니다. 옆의 다른 타일은
    # 모두 '판례 개수'이므로, 여기서도 중복을 제거한 판례 수를 보여 줘야
    # 타일을 눌렀을 때 나오는 목록과 숫자가 맞습니다.
    pending_total = len(data["pending_keys"])

    banner = ""
    sample_n = sum(1 for a in alerts if a.precedent.is_sample)
    if sample_n:
        banner += (
            f'<div class="banner"><b>⚠ 샘플 데이터 {sample_n}건이 섞여 있습니다</b><br>'
            f"봇 동작 확인용으로 넣은 합성 데이터입니다. 실제 판례가 아니고 "
            f"Claude가 분석한 결과도 아닙니다. <code>샘플</code> 표시로 구분됩니다.</div>"
        )
    if data["law_ok_ip"] and data["public_ip"] and data["law_ok_ip"] != data["public_ip"]:
        banner = (
            f'<div class="banner"><b>⚠ 공인 IP가 바뀌었습니다</b><br>'
            f'{_e(data["law_ok_ip"])} → <code>{_e(data["public_ip"])}</code><br>'
            f'open.law.go.kr에 새 IP를 등록해야 판례 수집이 재개됩니다.</div>'
        )

    q = f"?t={_e(token)}&" if token else "?"

    def stat(value: int, label: str, href: str) -> str:
        return f'<a class="stat" href="{href}"><b>{value}</b><span>{label}</span></a>'

    stats = (
        '<div class="stats">'
        + stat(c["precedents"], "수집", f"/all{q}f=all")
        + stat(c["analyses"], "분석", f"/all{q}f=all")
        + stat(len(alerts), "알림 대상", f"/all{q}f=alert")
        + stat(pending_total, "미발송", f"/all{q}f=pending")
        + "</div>"
    )

    if not alerts:
        cards = (
            '<div class="empty">아직 알림 대상이 없습니다.<br>'
            "수집·분석이 돌면 여기에 표시됩니다.</div>"
        )
    else:
        pending_keys = data["pending_keys"]
        cards = "".join(
            _card(
                a.precedent, a.analysis, token,
                score=a.score,
                badges=(
                    '<span class="tag smp">미발송</span>'
                    if a.precedent.key in pending_keys else ""
                ),
                companies=[m.company for m in a.matches],
            )
            for a in alerts
        )

    # 알림 목록에서 바로 세목·업종을 좁혀 볼 수 있게 상단에 축을 깔아 둡니다.
    # 실제 필터링은 /all 한 곳에서만 하므로 여기서는 이동 링크입니다.
    quick = _quick_axes(data, token, mode="alert")

    return _page("알림 · 세무 경정청구", banner + stats + quick + cards, "home", token)


def _quick_axes(data: dict[str, Any], token: str | None, mode: str) -> str:
    """알림 목록의 세목·업종을 몇 개만 뽑아 보여 주는 바로가기 줄.

    화면 장식이라 태그가 없으면 조용히 빠집니다. 이것 때문에 알림 목록
    자체가 안 그려지면 곤란합니다.
    """
    alerts: list[Alert] = data.get("alerts") or []
    tags: dict[str, dict[str, list[str]]] = data.get("tags") or {}
    if not alerts or not tags:
        return ""
    blocks = []
    for kind, label, limit in (("tax", "세목", 6), ("industry", "업종", 8)):
        counts: dict[str, int] = {}
        for alert in alerts:
            for value in tags.get(alert.precedent.key, {}).get(kind, ()):
                counts[value] = counts.get(value, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        if not top:
            continue
        chips = "".join(
            f'<a class="tchip" href="/all'
            f'{_query_string(token, mode, {}, "", add=(kind, value))}">'
            f"{_e(value)}<i>{n}</i></a>"
            for value, n in top
        )
        blocks.append(
            f'<div class="facet"><span class="fname">{label}</span>'
            f'<div class="tchips">{chips}</div></div>'
        )
    if not blocks:
        return ""
    return f'<div class="quick">{"".join(blocks)}</div>'


def render_browse(data: dict[str, Any], token: str | None) -> bytes:
    """세목·업종·주제·결과를 축별로 훑어보는 색인 화면."""
    q = f"?t={_e(token)}" if token else ""
    alert_keys = {a.precedent.key for a in data["alerts"]}

    sections = []
    for kind, label in TAG_KINDS.items():
        counts: dict[str, tuple[int, int]] = {}
        for r in data["rows"]:
            hit = r["prec"].key in alert_keys
            for value in r["tags"].get(kind, ()):
                total, alerted = counts.get(value, (0, 0))
                counts[value] = (total + 1, alerted + (1 if hit else 0))
        if not counts:
            continue
        # 알림 대상이 많은 축을 위로. 같으면 전체 건수, 그다음 이름순.
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))
        rows = "".join(
            f'<a class="brow" href="/all'
            f'{_query_string(token, "all", {}, "", add=(kind, value))}">'
            f'<span class="bname">{_e(value)}</span>'
            f'<span class="bnum">{total}건'
            f'{f" · 알림 {alerted}" if alerted else ""}</span></a>'
            for value, (total, alerted) in ordered[:BROWSE_LIMIT]
        )
        more = ""
        if len(ordered) > BROWSE_LIMIT:
            more = (
                f'<div class="bmore">이 밖에 {len(ordered) - BROWSE_LIMIT}종이 '
                f"더 있습니다. 검색으로 찾아보세요.</div>"
            )
        sections.append(
            f'<section><h3>{label} ({len(ordered)}종)</h3>'
            f'<div class="blist">{rows}</div>{more}</section>'
        )

    head = (
        '<div class="lead">보고 싶은 축을 누르면 그 조건으로 좁힌 목록이 열립니다.<br>'
        f'<a class="urltext" href="/all{q}">전체 목록에서 직접 검색</a>하거나 '
        "여러 축을 겹쳐 걸 수도 있습니다.</div>"
    )
    if not sections:
        return _page("분류", head + '<div class="empty">아직 분류할 분석이 없습니다.</div>',
                     "browse", token)
    return _page("분류 · 세목/업종/주제", head + "".join(sections), "browse", token)


FILTERS = (
    ("all", "전체"),
    ("alert", "알림 대상"),
    ("pending", "미발송"),
    ("rejected", "제외됨"),
)


def _filter_rows(
    rows: list[dict[str, Any]],
    mode: str,
    *,
    picked: dict[str, list[str]] | None = None,
    text: str = "",
) -> list[dict[str, Any]]:
    """상태 · 태그 · 검색어를 차례로 좁혀 갑니다.

    같은 종류의 태그를 여러 개 고르면 OR(세목이 법인세 '또는' 부가세),
    다른 종류끼리는 AND(세목이 법인세 '이면서' 업종이 제조업)입니다.
    한 종류 안에서 AND를 걸면 거의 항상 0건이 나와 쓸모가 없습니다.
    """
    if mode == "alert":
        rows = [r for r in rows if r["passed"]]
    elif mode == "pending":
        rows = [r for r in rows if r["pending"]]
    elif mode == "rejected":
        rows = [r for r in rows if not r["passed"]]

    for kind, values in (picked or {}).items():
        wanted = {v for v in values if v}
        if not wanted:
            continue
        rows = [r for r in rows if wanted & set(r["tags"].get(kind, ()))]

    for word in text.lower().split():
        rows = [r for r in rows if word in r["haystack"]]

    return rows


def _query_string(
    token: str | None, mode: str, picked: dict[str, list[str]], text: str,
    *, drop: tuple[str, str] | None = None, add: tuple[str, str] | None = None,
) -> str:
    """현재 필터를 유지한 채 태그 하나만 켜고 끄는 링크를 만듭니다."""
    parts: list[tuple[str, str]] = []
    if token:
        parts.append(("t", token))
    if mode and mode != "all":
        parts.append(("f", mode))
    for kind in TAG_KINDS:
        for value in picked.get(kind, []):
            if drop == (kind, value):
                continue
            parts.append((kind, value))
    if add and add != drop:
        parts.append(add)
    if text:
        parts.append(("q", text))
    return ("?" + urlencode(parts)) if parts else ""


def _facet_chips(
    data: dict[str, Any], token: str | None, mode: str,
    picked: dict[str, list[str]], text: str,
) -> str:
    """세목·업종·쟁점·결과 태그 줄. 누르면 켜지고 다시 누르면 꺼집니다."""
    blocks = []
    for kind, label in TAG_KINDS.items():
        options = data["facets"].get(kind) or []
        chosen = set(picked.get(kind, []))
        # 고른 태그가 상위 N개 밖에 있을 수 있습니다. 그것까지 못 보여 주면
        # 눌러서 끌 수가 없습니다.
        known = {value for value, _ in options}
        options = options + [(v, 0) for v in sorted(chosen - known)]
        if not options:
            continue
        chips = []
        for value, count in options:
            on = value in chosen
            href = _query_string(
                token, mode, picked, text,
                drop=(kind, value) if on else None,
                add=None if on else (kind, value),
            )
            suffix = f'<i>{count}</i>' if count else ""
            chips.append(
                f'<a href="/all{href}" class="tchip{" on" if on else ""}">'
                f"{_e(value)}{suffix}</a>"
            )
        blocks.append(
            f'<div class="facet"><span class="fname">{label}</span>'
            f'<div class="tchips">{"".join(chips)}</div></div>'
        )
    return "".join(blocks)


def render_all(
    data: dict[str, Any], token: str | None, mode: str = "all",
    picked: dict[str, list[str]] | None = None, text: str = "",
) -> bytes:
    picked = picked or {}
    rows = _filter_rows(data["rows"], mode, picked=picked, text=text)
    q = f"?t={_e(token)}" if token else ""

    chips = "".join(
        f'<a href="/all{_query_string(token, key, picked, text)}"'
        f' class="chip{" on" if key == mode else ""}">{label}</a>'
        for key, label in FILTERS
    )

    hidden = "".join(
        f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
        for k, v in (
            [("t", token)] if token else []
        ) + ([("f", mode)] if mode != "all" else []) + [
            (kind, value) for kind in TAG_KINDS for value in picked.get(kind, [])
        ]
    )
    search = (
        f'<form class="search" method="get" action="/all">{hidden}'
        f'<input type="search" name="q" value="{_e(text)}" '
        f'placeholder="제목 · 쟁점 · 사건번호 검색" autocomplete="off">'
        f'<button type="submit">검색</button></form>'
    )

    active = [(kind, v) for kind in TAG_KINDS for v in picked.get(kind, [])]
    if active or text:
        clear = f'<a class="clear" href="/all{q}">필터 해제</a>'
        summary = (
            f'<div class="fsum">{len(rows)}건'
            f'{" · 검색 " + _e(text) if text else ""}'
            f'{" · 태그 " + str(len(active)) + "개" if active else ""}'
            f"{clear}</div>"
        )
    else:
        summary = f'<div class="fsum">{len(rows)}건</div>'

    header = (
        f'<div class="chips">{chips}</div>{search}'
        f'{_facet_chips(data, token, mode, picked, text)}{summary}'
    )

    if not rows:
        if active or text:
            empty = "조건에 맞는 판례가 없습니다. 태그를 줄이거나 검색어를 바꿔 보세요."
        else:
            empty = {
                "alert": "알림 기준을 통과한 건이 없습니다.",
                "pending": "미발송 알림이 없습니다. 모두 전송되었습니다.",
                "rejected": "제외된 건이 없습니다.",
            }.get(mode, "분석된 판례가 없습니다.")
        return _page("전체", header + f'<div class="empty">{empty}</div>', "all", token)

    cards = []
    for r in rows:
        p, a, passed = r["prec"], r["analysis"], r["passed"]
        badge = (
            '<span class="tag co">알림</span>'
            if passed
            else f'<span class="tag no">{_e(r["reason"])}</span>'
        )
        if r["pending"]:
            badge += '<span class="tag smp">미발송</span>'
        badge += _row_tag_html(r)
        cards.append(
            _card(
                p, a, token,
                score=a.refund_confidence,
                passed=passed,
                badges=badge,
                summary=False,
            )
        )
    return _page("전체 분석", header + "".join(cards), "all", token)


def _row_tag_html(row: dict[str, Any]) -> str:
    """카드에 붙는 상태 배지.

    세목·업종·주제는 _axis_chips가 누를 수 있는 칩으로 따로 붙입니다.
    여기서는 그 축으로 표현할 수 없는 것(본문 출처, 중복)만 남깁니다.
    """
    out = ""
    if row["prec"].body_source == "nts":
        out += '<span class="tag src">국세청 본문</span>'
    if row.get("duplicate"):
        out += '<span class="tag src">중복(같은 판결)</span>'
    return out


def _body_source_label(prec: Precedent) -> str:
    """본문을 어디서 받았는지. 확인하러 갈 곳이 달라서 표시합니다."""
    if prec.body_source == "nts":
        return "국세법령정보시스템 (국세청)"
    if prec.body:
        return "국가법령정보센터"
    return "본문 없음 (제목·요지만)"


def _detail_tag_links(prec: Precedent, analysis: Analysis, token: str | None) -> str:
    """상세 화면의 태그. 누르면 그 태그로 거른 목록으로 갑니다."""
    groups: list[tuple[str, list[str]]] = [
        ("tax", [prec.resolved_tax_type] if prec.resolved_tax_type else []),
        ("industry", list(analysis.applicable_industries)),
        ("issue", list(analysis.issue_tags)),
        ("outcome", [analysis.outcome] if analysis.outcome else []),
    ]
    chips = []
    for kind, values in groups:
        for value in values:
            value = str(value).strip()
            if not value:
                continue
            href = _query_string(token, "all", {}, "", add=(kind, value))
            chips.append(f'<a class="tchip" href="/all{href}">{_e(value)}</a>')
    return "".join(chips) or '<span style="color:var(--sub)">태그 없음</span>'


def render_detail(alert: Alert, token: str | None) -> bytes:
    p, a = alert.precedent, alert.analysis
    q = f"?t={_e(token)}" if token else ""
    kind = "판례" if p.source == "prec" else "법령해석례"

    companies = "".join(
        f"<li><b>{_e(m.company)}</b> ({_e(m.industry or '업종 미기재')}) — {m.score}점"
        f"<br><span style='font-size:13px;color:var(--sub)'>{_e('; '.join(m.reasons))}</span></li>"
        for m in alert.matches
    ) or "<li>매칭된 회사 없음</li>"

    checks = "".join(f"<li>{_e(c)}</li>" for c in a.check_points) or "<li>-</li>"
    deadlines = "".join(f"<li>{_e(d)}</li>" for d in alert.deadlines)
    link = (
        f'<section><h3>원문</h3>'
        f'<a href="{_e(p.url)}" target="_blank" rel="noopener" class="urltext">'
        f"{_e(p.url)}</a></section>"
        if p.url.startswith("http")
        else ""
    )

    sample_note = (
        '<div class="banner"><b>⚠ 샘플 데이터</b><br>봇 동작 확인용 합성 데이터입니다. '
        "실제 판례가 아니며 Claude가 분석한 결과도 아닙니다.</div>"
        if p.is_sample
        else ""
    )

    body = f"""<a class="back" href="/{q}">← 목록</a>
{sample_note}
{_copy_data("cp-full", detail_copy_text(alert))}
<section>
<h2 style="margin:0 0 10px;font-size:18px">
<span class="score {_score_class(alert.score)}">{alert.score}</span>{_e(alert.title)}</h2>
<dl class="kv">
<dt>사건명</dt><dd>{_e(p.title)}</dd>
<dt>구분</dt><dd>{_e(kind)} · {_e(p.court or '-')}</dd>
<dt>사건번호</dt><dd>{_e(p.case_no or '-')}</dd>
<dt>선고일</dt><dd>{_e(p.decided_str)}</dd>
<dt>세목</dt><dd>{_e(p.resolved_tax_type or '-')}</dd>
<dt>결론</dt><dd>{_e(a.outcome)}</dd>
<dt>청구 근거</dt><dd>{_e(a.claim_basis)}</dd>
<dt>영향도</dt><dd>{_e(a.estimated_impact)}</dd>
<dt>본문 출처</dt><dd>{_e(_body_source_label(p))}</dd>
</dl>
<div class="actions">{_copy_button("cp-full", "복사")}</div>
</section>
<section><h3>태그</h3><div class="tchips" style="flex-wrap:wrap;overflow:visible">
{_detail_tag_links(p, a, token)}</div>
<div style="margin-top:8px;font-size:12px;color:var(--sub)">
태그를 누르면 같은 태그가 붙은 판례만 모아 봅니다.</div></section>
<section><h3>쟁점 요약</h3>
<div>{_e(a.issue_summary)}</div></section>
<section><h3>경정청구 실익</h3>
<div>{_e(a.refund_rationale)}</div>
<div style="margin-top:10px;font-size:13px;color:var(--sub)">
일반화 가능성: {'예' if a.generalizable else '아니오'} — {_e(a.generalizable_reason)}</div></section>
<section><h3>대상 회사</h3><ul>{companies}</ul></section>
<section><h3>확인할 것</h3><ul>{checks}</ul></section>
<section><h3>청구 기한</h3><ul>{deadlines}</ul></section>
{link}
<section><h3>유의사항</h3><div style="font-size:13px;color:var(--sub)">{_e(a.caveats)}</div></section>"""
    return _page(p.title, body, "home", token)


def render_status(data: dict[str, Any], token: str | None) -> bytes:
    cfg: Config = data["cfg"]
    c = data["counts"]
    pending = "".join(
        f"<dt>{_e(ch)}</dt><dd>{n}건 미발송</dd>" for ch, n in data["pending"].items()
    )
    watch = "".join(
        f"<li><b>{_e(w.name)}</b> — {_e(w.industry or '-')}"
        f"{(' · KSIC ' + ', '.join(w.ksic)) if w.ksic else ''}</li>"
        for w in cfg.watchlist
    ) or "<li>워치리스트 비어 있음</li>"

    ip_row = _e(data["public_ip"] or "미확인")
    if data["law_ok_ip"] and data["public_ip"] and data["law_ok_ip"] != data["public_ip"]:
        ip_row += f' <span class="tag no">등록된 IP({_e(data["law_ok_ip"])})와 다름</span>'

    schedule = (
        f"매일 {cfg.serve.daily_at}" if cfg.serve.daily_at
        else f"{cfg.serve.interval_minutes}분마다"
    )

    served = tailscale_https_url(cfg.web.port)
    ts_name = tailscale_dns_name()
    https_note = ""

    if served:
        https_note = f"""<section><h3>접속 주소</h3>
<div style="font-size:14px">✅ HTTPS로 서비스 중입니다. 복사 버튼이 한 번에 동작하고,
접근 토큰도 암호화되어 오갑니다.</div>
<div style="margin-top:10px"><a href="{_e(served)}" class="urltext">{_e(served)}</a></div>
<div style="font-size:12px;color:var(--sub);margin-top:10px">
대시보드는 127.0.0.1에만 묶여 있고 Tailscale Serve만 통해 노출됩니다.
같은 tailnet의 본인 기기에서만 접근됩니다.</div></section>"""
    elif ts_name:
        https_note = f"""<section><h3>한 번 눌러 복사되게 하기</h3>
<div style="font-size:14px">지금은 <b>HTTP</b>라 브라우저가 자동 복사를 막습니다
(특히 iPhone). Tailscale HTTPS를 켜면 복사 버튼이 한 번에 동작합니다.</div>
<ol style="font-size:14px;margin-top:10px;padding-left:22px">
<li><a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noopener"
   class="urltext">login.tailscale.com/admin/dns</a> 접속</li>
<li><b>HTTPS Certificates</b> → <b>Enable</b> (무료)</li>
<li>PC에서 아래 한 줄 실행<br>
<code>tailscale serve --bg --https=443 http://127.0.0.1:8787</code></li>
<li>이후 접속 주소:<br><code>https://{_e(ts_name)}/</code></li>
</ol>
<div style="font-size:12px;color:var(--sub);margin-top:10px">
설정하면 토큰도 암호화되어 오갑니다. 자세한 절차는 README의 '폰에서 보기'를 참고하세요.
</div></section>"""

    body = f"""{https_note}<section><h3>수집 현황</h3><dl class="kv">
<dt>수집 판례</dt><dd>{c['precedents']}건</dd>
<dt>분석 완료</dt><dd>{c['analyses']}건</dd>
<dt>알림 발송</dt><dd>{c['notifications']}건</dd>
<dt>마지막 실행</dt><dd>{_e(data['last_run'] or '없음')}</dd>
{pending}</dl></section>
<section><h3>설정</h3><dl class="kv">
<dt>일정</dt><dd>{_e(schedule)}</dd>
<dt>최소 점수</dt><dd>{cfg.notify.min_confidence}점</dd>
<dt>알림 채널</dt><dd>{_e(', '.join(cfg.notify.channels))}</dd>
<dt>디스코드</dt><dd>{_e(cfg.discord_mode or '미설정')}</dd>
<dt>공인 IP</dt><dd>{ip_row}</dd>
</dl></section>
<section><h3>워치리스트</h3><ul>{watch}</ul></section>"""
    return _page("상태", body, "status", token)


# ------------------------------------------------------------------ 서버

class DashboardHandler(BaseHTTPRequestHandler):
    cfg: Config
    token: str | None = None

    server_version = "taxrefund-web"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 기본 http.server 로그는 잠재웁니다. 아래 _log_access가 대신합니다.
        log.debug("web %s", fmt % args)

    def _log_access(self, status: int) -> None:
        """누가 접속했는지 로그에 남깁니다.

        '폰에서 접속이 안 된다'고 할 때, 요청이 서버까지 왔는지(=네트워크 문제인지)
        아니면 토큰 때문에 거절된 건지(=401) 구분하려면 이 기록이 꼭 필요합니다.
        """
        client = self.client_address[0] if self.client_address else "?"
        if status == 401:
            log.warning("대시보드 접속 거절(토큰 없음/불일치): %s -> %s", client, self.path)
        elif status >= 400:
            log.warning("대시보드 %s: %s -> %s", status, client, self.path)
        elif not self.path.startswith("/health"):
            log.info("대시보드 접속: %s -> %s", client, self.path.split("?")[0])

    # -- 인증 ---------------------------------------------------------
    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not self.token:
            return True
        if query.get("t", [""])[0] == self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"{TOKEN_COOKIE}={self.token}" in cookie

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
              set_cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)
        self._log_access(status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send(b'{"status":"ok"}', ctype="application/json")
            return

        if not self._authorized(query):
            # 토큰을 빠뜨린 게 가장 흔한 원인이라, 붙일 위치를 그대로 보여줍니다.
            example = f"{self.path.split('?')[0]}?t=<토큰>"
            self._send(
                _page(
                    "인증 필요",
                    '<div class="empty">접근 토큰이 필요합니다.<br><br>'
                    f"주소 끝에 <code>?t=토큰</code>을 붙이세요.<br>"
                    f"<code>{_e(example)}</code><br><br>"
                    "토큰은 프로젝트의 <code>.env</code> 파일 "
                    "<code>WEB_TOKEN</code> 값입니다.</div>",
                    "",
                    None,
                ),
                status=401,
            )
            return

        cookie = None
        if self.token and query.get("t", [""])[0] == self.token:
            cookie = f"{TOKEN_COOKIE}={self.token}; Path=/; Max-Age=31536000; SameSite=Lax"

        try:
            if path == "/":
                body = render_home(gather(self.cfg), self.token)
            elif path == "/all":
                mode = query.get("f", ["all"])[0]
                if mode not in {key for key, _ in FILTERS}:
                    mode = "all"
                picked = {
                    kind: [v for v in query.get(kind, []) if v.strip()]
                    for kind in TAG_KINDS
                }
                text = query.get("q", [""])[0].strip()
                body = render_all(gather(self.cfg), self.token, mode, picked, text)
            elif path == "/browse":
                body = render_browse(gather(self.cfg), self.token)
            elif path == "/status":
                body = render_status(gather(self.cfg), self.token)
            elif path.startswith("/d/"):
                alert = alert_by_key(self.cfg, path[3:])
                if alert is None:
                    self._send(
                        _page("없음", '<div class="empty">찾을 수 없습니다.</div>', "home", self.token),
                        status=404,
                    )
                    return
                body = render_detail(alert, self.token)
            elif path == "/api/alerts.json":
                data = gather(self.cfg)
                payload = [
                    {
                        "key": a.precedent.key,
                        "score": a.score,
                        "title": a.precedent.title,
                        "court": a.precedent.court,
                        "case_no": a.precedent.case_no,
                        "decided_on": a.precedent.decided_str,
                        "url": a.precedent.url,
                        "tax_type": a.precedent.resolved_tax_type,
                        "body_source": a.precedent.body_source or "lawgo",
                        "companies": [m.company for m in a.matches],
                        "analysis": a.analysis.to_dict(),
                        "deadlines": a.deadlines,
                    }
                    for a in data["alerts"]
                ]
                self._send(
                    json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    ctype="application/json; charset=utf-8",
                    set_cookie=cookie,
                )
                return
            else:
                self._send(
                    _page("없음", '<div class="empty">없는 페이지입니다.</div>', "home", self.token),
                    status=404,
                )
                return
        except Exception as exc:
            log.exception("대시보드 렌더링 실패: %s", exc)
            self._send(
                _page("오류", f'<div class="empty">오류가 발생했습니다.<br>{_e(exc)}</div>',
                      "home", self.token),
                status=500,
            )
            return

        self._send(body, set_cookie=cookie)


def outbound_ip() -> str:
    """바깥으로 나갈 때 쓰는 이 PC의 주소. NAT 뒤면 사설 IP, 아니면 공인 IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _is_cgnat(ip: str) -> bool:
    """100.64.0.0/10 — Tailscale이 쓰는 대역."""
    parts = ip.split(".")
    return len(parts) == 4 and parts[0] == "100" and parts[1].isdigit() and 64 <= int(parts[1]) <= 127


def tailscale_dns_name() -> str | None:
    """이 기기의 Tailscale DNS 이름 (예: home.tailxxxx.ts.net).

    HTTPS 안내에 쓰므로, 못 찾으면 안내를 생략합니다.
    """
    if not TAILSCALE_EXE.exists():
        return None
    try:
        out = subprocess.run(
            [str(TAILSCALE_EXE), "status", "--json"],
            capture_output=True, text=True, timeout=8, encoding="utf-8",
        )
        if out.returncode != 0:
            return None
        name = (json.loads(out.stdout).get("Self") or {}).get("DNSName") or ""
        return name.rstrip(".") or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _tailscale_serve_config() -> dict | None:
    """`tailscale serve status --json`. 실패하면 None."""
    if not TAILSCALE_EXE.exists():
        return None
    try:
        out = subprocess.run(
            [str(TAILSCALE_EXE), "serve", "status", "--json"],
            capture_output=True, text=True, timeout=8, encoding="utf-8",
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def tailscale_https_url(port: int) -> str | None:
    """Tailscale Serve가 이 포트를 HTTPS로 프록시하고 있으면 그 주소.

    대시보드를 127.0.0.1에만 묶고 Serve로 노출하는 구성에서, 실제 접속
    주소는 https://<기기>.<tailnet>.ts.net 입니다. 이걸 못 찾으면
    시작 로그가 접속되지 않는 주소를 안내하게 됩니다.
    """
    conf = _tailscale_serve_config()
    if conf is None:
        return None
    return _url_for_port(conf, port)


def _url_for_port(conf: dict, port: int) -> str | None:
    target = f"http://127.0.0.1:{port}"
    for host_port, entry in (conf.get("Web") or {}).items():
        for path, handler in (entry.get("Handlers") or {}).items():
            if handler.get("Proxy") != target:
                continue
            host, _, hp = host_port.rpartition(":")
            if hp == "443":
                return f"https://{host}{path if path != '/' else '/'}"
            return f"https://{host}:{hp}{path if path != '/' else '/'}"
    return None


def _serve_slot_owner(conf: dict, https_port: int) -> str | None:
    """<host>:<https_port>를 이미 쓰고 있는 프록시 대상. 비어 있으면 None."""
    for host_port, entry in (conf.get("Web") or {}).items():
        if host_port.rpartition(":")[2] != str(https_port):
            continue
        for handler in (entry.get("Handlers") or {}).values():
            if handler.get("Proxy"):
                return handler["Proxy"]
    return None


def ensure_tailscale_serve(port: int, https_port: int = 443) -> tuple[str | None, bool]:
    """Serve 매핑이 사라졌으면 다시 붙입니다. -> (접속주소, 복구했는가)

    `tailscale serve` 설정은 프로젝트별이 아니라 **노드 전체에 하나**입니다.
    같은 PC의 다른 프로젝트가 설정을 덮어쓰거나 reset하면 이 대시보드의
    매핑만 조용히 사라져서, 서버는 멀쩡한데 폰에서만 접속이 끊깁니다.

    남의 매핑을 빼앗지는 않습니다. https_port를 다른 대상이 쓰고 있으면
    그대로 두고 물러납니다.
    """
    conf = _tailscale_serve_config()
    if conf is None:
        return None, False

    url = _url_for_port(conf, port)
    if url:
        return url, False

    owner = _serve_slot_owner(conf, https_port)
    if owner:
        log.warning(
            "Tailscale :%d 는 이미 %s 가 쓰고 있어 대시보드를 붙이지 않았습니다.",
            https_port, owner,
        )
        return None, False

    try:
        out = subprocess.run(
            [str(TAILSCALE_EXE), "serve", "--bg", f"--https={https_port}",
             f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Tailscale serve 복구 실패: %s", exc)
        return None, False

    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()[:300]
        log.warning("Tailscale serve 복구 실패: %s", detail)
        return None, False

    # 명령이 성공해도 실제로 붙었는지는 다시 확인해야 신뢰할 수 있습니다.
    restored = tailscale_https_url(port)
    if restored:
        log.info("Tailscale serve 매핑을 복구했습니다: %s", restored)
    return restored, bool(restored)


def tailscale_ip() -> str | None:
    """Tailscale 인터페이스 주소. 없으면 None."""
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return None
    return next((ip for ip in addrs if _is_cgnat(ip)), None)


def is_private(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or a == 127
        or _is_cgnat(ip)
    )


def resolve_host(cfg: Config) -> str:
    """web.host의 별칭을 실제 바인드 주소로 바꿉니다.

    - "tailscale": Tailscale 주소에만 바인드 (외부에 노출되지 않음). 없으면 127.0.0.1.
    - "local"    : 127.0.0.1 (이 PC에서만)
    - 그 외      : 값 그대로
    """
    host = (cfg.web.host or "").strip().lower()
    if host == "tailscale":
        ip = tailscale_ip()
        if ip:
            return ip
        log.warning("Tailscale 주소를 찾지 못해 127.0.0.1로 바인드합니다.")
        return "127.0.0.1"
    if host in ("local", "localhost"):
        return "127.0.0.1"
    return cfg.web.host


def make_server(cfg: Config) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (DashboardHandler,),
        {"cfg": cfg, "token": cfg.secrets.web_token},
    )
    server = ThreadingHTTPServer((resolve_host(cfg), cfg.web.port), handler)
    server.daemon_threads = True
    return server


def exposure_warning(cfg: Config) -> str | None:
    """대시보드가 인터넷에 열려 있으면 경고 문구. 안전하면 None."""
    host = resolve_host(cfg)
    if host not in ("0.0.0.0", "::"):
        return None if is_private(host) else (
            f"대시보드가 공인 IP({host})에 바인드되어 인터넷에서 접근 가능합니다."
        )
    out = outbound_ip()
    if not is_private(out):
        return (
            f"이 PC는 공인 IP({out})를 직접 쓰고 있고 대시보드가 0.0.0.0에 바인드되어 "
            f"인터넷에서 접근 가능합니다. config.yaml의 web.host를 'tailscale' 또는 "
            f"'local'로 바꾸는 것을 권합니다."
        )
    return None


def access_urls(cfg: Config) -> list[str]:
    """실제로 접속되는 주소만 돌려줍니다.

    특정 IP에만 바인드했을 때 localhost는 접속되지 않습니다.
    안내에 넣으면 "접속이 안 된다"는 오해를 부르므로 제외합니다.
    """
    q = f"?t={cfg.secrets.web_token}" if cfg.secrets.web_token else ""
    host = resolve_host(cfg)
    urls: list[str] = []

    # Tailscale Serve로 HTTPS 노출 중이면 그게 실제 접속 주소입니다.
    served = tailscale_https_url(cfg.web.port)
    if served:
        urls.append(f"{served}{q}   ← 폰 (HTTPS)")

    if host in ("0.0.0.0", "::"):
        urls.append(f"http://localhost:{cfg.web.port}{q}")
        ts = tailscale_ip()
        if ts:
            urls.append(f"http://{ts}:{cfg.web.port}{q}   ← 폰(Tailscale)")
        urls.append(f"http://{outbound_ip()}:{cfg.web.port}{q}   ← 같은 네트워크")
    elif host == "127.0.0.1":
        urls.append(f"http://localhost:{cfg.web.port}{q}   ← 이 PC에서만")
    else:
        label = "폰(Tailscale)" if _is_cgnat(host) else "폰"
        # 이 주소로만 바인드되어 있으므로 localhost는 안내하지 않습니다.
        urls.append(f"http://{host}:{cfg.web.port}{q}   ← {label} / 이 PC 모두")
    return urls


def serve_forever(cfg: Config) -> None:
    server = make_server(cfg)
    warning = exposure_warning(cfg)
    if warning:
        print(f"  ⚠ {warning}")
    for url in access_urls(cfg):
        print(f"  {url}")
    log.info("대시보드 시작: %s:%s", resolve_host(cfg), cfg.web.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def start_in_thread(cfg: Config) -> ThreadingHTTPServer | None:
    """serve 명령과 함께 백그라운드로 띄웁니다."""
    try:
        server = make_server(cfg)
    except OSError as exc:
        log.error("대시보드를 시작하지 못했습니다 (%s:%s): %s",
                  resolve_host(cfg), cfg.web.port, exc)
        return None
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    warning = exposure_warning(cfg)
    if warning:
        log.warning("%s", warning)
    log.info("대시보드 시작: %s", " | ".join(access_urls(cfg)))
    return server
