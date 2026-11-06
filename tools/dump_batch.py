"""분석하지 않은 판례를 읽기 좋은 형태로 꺼냅니다.

`import_analysis.py`의 짝입니다. 대화형으로 판례를 읽어 분석할 때, 매번
SQL을 두드리지 않고 다음 묶음을 바로 받아 보려고 만들었습니다.

    python tools/dump_batch.py --limit 10                 # 다음 10건
    python tools/dump_batch.py --limit 10 --chars 3000    # 본문을 더 길게
    python tools/dump_batch.py --keys prec:1 prec:2       # 특정 건만
    python tools/dump_batch.py --list --limit 40          # 제목만 훑기

본문은 앞부분만 자릅니다. 판결문은 '주문'과 '이유 1. 사안의 개요'가 앞에
오고 쟁점 판단이 그 뒤에 붙어서, 앞부분만으로 부족하면 `--chars`를
올리거나 `--keys`로 그 건만 길게 다시 봅니다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.config import load_config  # noqa: E402
from taxrefund.store import Store  # noqa: E402

# 판결문 앞머리의 형식적인 부분. 쟁점 판단이 아니라 자리만 차지합니다.
_NOISE = re.compile(
    r"^\s*(원고,?\s*(상고인|피상고인|항소인|피항소인)?|피고,?\s*(상고인|피상고인|항소인|피항소인)?"
    r"|소송대리인|변 론 종 결|변론종결|판 결 선 고|판결선고|주\s*문|이\s*유)\s*$"
)


def squeeze(text: str, limit: int) -> str:
    """빈 줄과 형식적인 머리말을 줄여 실질 내용을 더 담습니다."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lines.append(line)
    out = "\n".join(lines)
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10, help="꺼낼 건수")
    ap.add_argument("--chars", type=int, default=2600, help="건당 본문 글자 수")
    ap.add_argument("--keys", nargs="+", help="이 키만 꺼냅니다")
    ap.add_argument("--list", action="store_true", help="제목만 한 줄씩")
    ap.add_argument("--tax", help="세목으로 거르기 (예: 법인세)")
    ap.add_argument("--config")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    cfg = load_config(args.config)
    with Store(cfg.db_path) as store:
        if args.keys:
            rows = [
                store.conn.execute(
                    "SELECT * FROM precedents WHERE key = ?", (k,)
                ).fetchone()
                for k in args.keys
            ]
            from taxrefund.store import _row_to_precedent

            precs = [_row_to_precedent(r) for r in rows if r is not None]
        else:
            precs = store.unanalyzed_precedents()
            if args.tax:
                precs = [p for p in precs if p.resolved_tax_type == args.tax]
            precs = precs[: args.limit]

        left = store.precedent_stats()["pending"]

    if not precs:
        print("분석할 판례가 없습니다.")
        return 0

    if args.list:
        for p in precs:
            print(
                f"{p.key}\t{p.decided_str}\t{p.resolved_tax_type or '-'}\t"
                f"{len(p.body)}자\t{p.title}"
            )
        print(f"\n# 대기 {left}건")
        return 0

    for i, p in enumerate(precs, 1):
        print(f"\n{'=' * 74}")
        print(f"[{i}/{len(precs)}] {p.key}")
        print(f"사건명 : {p.title}")
        print(
            f"메타   : {p.case_no or '-'} · {p.court or '-'} · {p.decided_str} · "
            f"세목 {p.resolved_tax_type or '-'} · 본문 {len(p.body)}자"
            f"{' · 국세청' if p.body_source == 'nts' else ''}"
        )
        if p.summary:
            print(f"판시요지: {p.summary.strip()[:600]}")
        print("-" * 74)
        print(squeeze(p.body, args.chars))

    print(f"\n{'=' * 74}")
    print(f"# 위 {len(precs)}건. 남은 대기 {left - len(precs)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
