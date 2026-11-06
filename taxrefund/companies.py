"""DART 기업 색인 구축·조회.

판례가 걸리는 업종을 알아냈을 때 그 업종의 외부감사대상 법인을 찾기 위해,
기업별 업종코드를 미리 긁어 SQLite에 쌓아 둡니다.

DART 일일 한도(20,000건)에 걸리면 그때까지 수집한 것을 저장하고 멈춥니다.
다음 날 같은 명령을 다시 실행하면 이어서 진행합니다.
"""

from __future__ import annotations

import logging

from .config import Config
from .dart import CORP_CLS, DartClient, DartError
from .store import META_DART_FILERS, Store

log = logging.getLogger(__name__)


def build_index(cfg: Config, *, limit: int | None = None, refresh_filers: bool = False) -> int:
    """기업 색인을 만들거나 이어서 채웁니다."""
    client = DartClient(cfg.secrets.dart_api_key or "")

    with Store(cfg.db_path) as store:
        cached = store.get_meta(META_DART_FILERS)
        if cached and not refresh_filers:
            filers = [c for c in cached.split(",") if c]
            print(f"  대상 기업 {len(filers):,}곳 (저장된 목록 사용)")
        else:
            print("  사업보고서 제출 기업 목록을 수집합니다 (몇 분 걸립니다)...")
            filers = sorted(client.annual_report_filers(years=2))
            store.set_meta(META_DART_FILERS, ",".join(filers))
            print(f"  대상 기업 {len(filers):,}곳 확인")

        done = store.indexed_corp_codes()
        todo = [c for c in filers if c not in done]
        if limit is not None:
            todo = todo[:limit]

        print(f"  이미 색인됨 {len(done):,}곳 / 남은 대상 {len(filers) - len(done):,}곳")
        if not todo:
            print("  색인이 최신입니다.")
            return 0

        print(f"  이번 실행에서 {len(todo):,}곳 수집 시작")
        batch, saved = [], 0
        for company in client.profiles(todo):
            batch.append(company)
            if len(batch) >= 200:
                saved += store.save_companies(batch)
                batch.clear()
                print(f"    {saved:,}곳 저장")
        if batch:
            saved += store.save_companies(batch)

        stats = store.company_stats()
        print(f"  이번 실행 {saved:,}곳 저장 / 누적 {stats['total']:,}곳")
        remaining = len(filers) - len(store.indexed_corp_codes())
        if remaining > 0:
            print(f"  남은 {remaining:,}곳은 내일 같은 명령으로 이어서 수집하세요.")
    return 0


def show_stats(cfg: Config) -> int:
    with Store(cfg.db_path) as store:
        stats = store.company_stats()
        if not stats["total"]:
            print("  색인된 기업이 없습니다. `python -m taxrefund companies build` 먼저 실행하세요.")
            return 1
        print(f"  총 {stats['total']:,}곳 (업종코드 보유 {stats['with_industry']:,}곳)")
        for cls, label in CORP_CLS.items():
            if cls in stats:
                print(f"    {label:<14} {stats[cls]:,}곳")
    return 0


def find(cfg: Config, ksic: list[str], *, limit: int = 30, audited_only: bool = False) -> int:
    with Store(cfg.db_path) as store:
        rows = store.companies_by_industry(ksic, limit=limit, audited_only=audited_only)
        if not rows:
            print(f"  업종코드 {', '.join(ksic)} 에 해당하는 기업이 없습니다.")
            print("  색인이 아직 부족할 수 있습니다: python -m taxrefund companies stats")
            return 1
        print(f"  업종코드 {', '.join(ksic)} — {len(rows)}곳")
        print("  " + "-" * 78)
        for r in rows:
            acc = f"{r['acc_mt']}월결산" if r["acc_mt"] else "결산월?"
            print(
                f"  {CORP_CLS.get(r['corp_cls'], '-'):<12} {(r['corp_name'] or '')[:22]:<22} "
                f"{r['induty_code']:<8} {acc:<9} {(r['adres'] or '')[:24]}"
            )
    return 0
