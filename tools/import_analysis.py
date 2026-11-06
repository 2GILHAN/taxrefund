"""사람(또는 대화형 LLM)이 직접 판단한 분석 결과를 DB에 넣습니다.

API를 쓰지 않고 판례를 읽어 분석했을 때 그 결과를 저장하는 통로입니다.
밀린 물량을 한 번 털 때 씁니다. 평소 자동 수집은 API 분석기가 맡습니다.

    python tools/import_analysis.py analyses.json
    python tools/import_analysis.py analyses.json --dry-run

JSON 형식 — 판례 키를 열쇠로 한 객체:

    {
      "prec:622311": { "headline": "...", "outcome": "과세관청승", ... },
      "prec:619437": { ... }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.analyzer import ANALYSIS_SCHEMA  # noqa: E402
from taxrefund.config import load_config  # noqa: E402
from taxrefund.models import Analysis  # noqa: E402
from taxrefund.store import Store  # noqa: E402

REQUIRED = ANALYSIS_SCHEMA["required"]
ENUMS = {
    name: spec["enum"]
    for name, spec in ANALYSIS_SCHEMA["properties"].items()
    if "enum" in spec
}


def validate(key: str, payload: dict) -> list[str]:
    """API 분석기와 같은 스키마를 지키는지 확인합니다.

    수동 입력이라고 형식이 느슨해지면 대시보드·매칭이 조용히 어긋납니다.
    """
    problems = []
    known = set(ANALYSIS_SCHEMA["properties"])
    for field in sorted(set(payload) - known):
        problems.append(f"스키마에 없는 항목: {field}")

    for field in REQUIRED:
        if field not in payload:
            problems.append(f"필수 항목 누락: {field}")

    for field, allowed in ENUMS.items():
        value = payload.get(field)
        if value is not None and value not in allowed:
            problems.append(f"{field}={value!r} — 허용값: {allowed}")

    score = payload.get("refund_confidence")
    if score is not None and not (isinstance(score, int) and 0 <= score <= 100):
        problems.append(f"refund_confidence={score!r} — 0~100 정수여야 합니다")

    for field in ("issue_tags", "applicable_industries", "ksic_codes", "check_points"):
        value = payload.get(field)
        if value is not None and not isinstance(value, list):
            problems.append(f"{field}는 배열이어야 합니다")

    return [f"[{key}] {p}" for p in problems]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="분석 결과 JSON")
    ap.add_argument("--dry-run", action="store_true", help="검증만 하고 저장하지 않음")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 분석된 건도 덮어씁니다 (자동 분석 결과를 수동 판단으로 교체할 때)",
    )
    args = ap.parse_args()

    data = json.loads(args.path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("최상위는 '판례키: 분석' 형태의 객체여야 합니다.", file=sys.stderr)
        return 2

    cfg = load_config()
    saved = skipped = 0
    with Store(cfg.db_path) as store:
        known = {p.key for p in store.analyzable_precedents()}

        problems: list[str] = []
        for key, payload in data.items():
            if key not in known:
                problems.append(f"[{key}] DB에 없는 판례이거나 본문이 없습니다")
            problems.extend(validate(key, payload))

        if problems:
            print(f"검증 실패 {len(problems)}건:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1

        for key, payload in data.items():
            if store.get_analysis(key) is not None and not args.overwrite:
                print(f"  건너뜀(이미 분석됨): {key}  — 바꾸려면 --overwrite")
                skipped += 1
                continue
            if not args.dry_run:
                store.save_analysis(key, Analysis.from_json(payload))
            saved += 1
            print(f"  {'[검증]' if args.dry_run else '저장'} {key} — {payload['headline'][:52]}")

        stats = store.precedent_stats()

    verb = "검증 통과" if args.dry_run else "저장"
    print(f"\n{verb} {saved}건 / 건너뜀 {skipped}건")
    print(f"분석 완료 {stats['analyzed']}건 · 대기 {stats['pending']}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
