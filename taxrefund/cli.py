"""커맨드라인 진입점."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime

from .analyzer import CredentialsError, QuotaExceededError
from .config import load_config
from .lawgo import LawApiError
from .matcher import compute_deadlines
from .models import Alert, Analysis, Match, Precedent
from .notifier import Notifier
from .notifier import STATUS_LABEL
from .pipeline import backfill, check, crawl, diagnose, headlines, run
from .serve import Server, setup_file_logging
from .store import Store
from .web import serve_forever


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _demo_alert() -> Alert:
    prec = Precedent(
        source="prec",
        doc_id="TEST-0000",
        title="[테스트] 알림 채널 점검용 가짜 판례",
        case_no="2025두99999",
        court="대법원",
        decided_on=date.today(),
        summary="알림 채널이 제대로 연결되었는지 확인하기 위한 테스트 메시지입니다.",
        body="",
        url="https://www.law.go.kr/",
    )
    analysis = Analysis(
        is_corporate_tax=True,
        outcome="납세자승",
        issue_summary="이 메시지가 보인다면 알림 채널 설정이 정상입니다.",
        issue_tags=["테스트"],
        applicable_industries=["전업종"],
        generalizable=True,
        generalizable_reason="테스트용",
        refund_claim_possible=True,
        refund_confidence=99,
        refund_rationale="테스트용 더미 데이터입니다.",
        claim_basis="통상적 경정청구",
        check_points=["알림이 도착했는지 확인"],
        estimated_impact="하",
        caveats="테스트 메시지입니다.",
    )
    return Alert(
        precedent=prec,
        analysis=analysis,
        matches=[Match(company="테스트회사", industry="테스트업", reasons=["채널 점검"], score=99)],
        deadlines=compute_deadlines(analysis, prec),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taxrefund",
        description="최근 법인세 판례를 수집해 동종업계 경정청구 가능성을 분석하고 알림을 보냅니다.",
    )
    parser.add_argument("-c", "--config", help="설정 파일 경로 (기본: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="수집 -> 분석 -> 알림 한 사이클 실행")
    p_run.add_argument("--sample", action="store_true", help="API 대신 동봉된 샘플 판례로 실행")
    p_run.add_argument("--limit", type=int, help="이번 실행에서 분석할 최대 건수")
    p_run.add_argument("--force", action="store_true", help="이미 처리한 건도 다시 분석/발송")
    p_run.add_argument("--dry-run", action="store_true", help="분석까지만 하고 발송하지 않음")
    p_run.add_argument("--channels", nargs="+", help="이번 실행에만 쓸 알림 채널")
    p_run.add_argument(
        "--every", type=int, metavar="MIN", help="N분마다 반복 실행 (미지정 시 1회만)"
    )

    p_serve = sub.add_parser("serve", help="상시 실행 서버 (일정에 맞춰 반복 실행)")
    p_serve.add_argument("--sample", action="store_true", help="API 대신 샘플 판례로 실행")
    p_serve.add_argument("--limit", type=int, help="사이클당 분석할 최대 건수")
    p_serve.add_argument("--force", action="store_true", help="이미 처리한 건도 다시 처리")
    p_serve.add_argument("--channels", nargs="+", help="알림 채널 덮어쓰기")
    p_serve.add_argument("--daily-at", metavar="HH:MM", help="매일 이 시각에 실행")
    p_serve.add_argument("--interval", type=int, metavar="MIN", help="N분마다 실행 (daily-at 대신)")
    p_serve.add_argument(
        "--no-run-on-start", action="store_true", help="시작 직후 즉시 실행하지 않음"
    )

    p_web = sub.add_parser("web", help="웹 대시보드만 실행 (폰에서 확인)")
    p_web.add_argument("--host", help="바인드 주소 (기본: 0.0.0.0)")
    p_web.add_argument("--port", type=int, help="포트 (기본: 8787)")

    p_back = sub.add_parser(
        "backfill",
        help="아직 못 보낸 알림을 몰아서 발송 (디스코드 봇을 나중에 붙였을 때)",
    )
    p_back.add_argument("--channels", nargs="+", help="대상 채널 (기본: config의 channels)")
    p_back.add_argument("--limit", type=int, help="채널당 최대 발송 건수")
    p_back.add_argument("--dry-run", action="store_true", help="목록만 보고 발송하지 않음")

    p_crawl = sub.add_parser(
        "crawl",
        help="판례를 받아 저장만 합니다 (분석 없음). 과거 구간 채우기에 씁니다",
    )
    p_crawl.add_argument(
        "--since-days", type=int, help="이 기간 안의 건을 모읍니다 (기본: config)"
    )
    p_crawl.add_argument("--max-pages", type=int, help="검색어당 최대 페이지 수")

    p_bodies = sub.add_parser(
        "bodies",
        help="본문이 빈 판례를 국세법령정보시스템에서 보충합니다",
    )
    p_bodies.add_argument("--limit", type=int, help="최대 처리 건수 (기본: config)")
    p_bodies.add_argument("--all", action="store_true", help="밀린 것 전부")

    sub.add_parser("status", help="수집·분석 진행 상황")

    sub.add_parser("check", help="API 키·워치리스트·알림 채널 설정 점검")
    sub.add_parser("diagnose", help="법령 OPEN API가 거부될 때 원인 좁히기")
    sub.add_parser("models", help="이 API 키로 쓸 수 있는 Gemini 모델 목록")

    p_head = sub.add_parser(
        "headlines", help="한 줄 요지가 없는 기존 분석에 제목을 채웁니다"
    )
    p_head.add_argument("--limit", type=int, help="최대 처리 건수")
    p_head.add_argument("--force", action="store_true", help="이미 있는 것도 다시 생성")

    p_co = sub.add_parser("companies", help="DART 기업 색인 (업종별 대상 회사 조회)")
    co_sub = p_co.add_subparsers(dest="co_command", required=True)

    co_build = co_sub.add_parser("build", help="색인 구축/이어서 채우기")
    co_build.add_argument("--limit", type=int, help="이번 실행에서 수집할 최대 기업 수")
    co_build.add_argument(
        "--refresh-filers", action="store_true", help="대상 기업 목록을 다시 수집"
    )

    co_sub.add_parser("stats", help="색인 현황")

    co_find = co_sub.add_parser("find", help="업종코드로 기업 조회")
    co_find.add_argument("ksic", nargs="+", help="업종코드 접두사 (예: 26 27)")
    co_find.add_argument("-n", "--limit", type=int, default=30)
    co_find.add_argument(
        "--audited-only", action="store_true", help="비상장 외감법인(기타)만"
    )

    p_list = sub.add_parser("list", help="최근 수집/분석한 판례 조회")
    p_list.add_argument("-n", "--limit", type=int, default=20)

    p_test = sub.add_parser("test-notify", help="가짜 알림을 보내 채널 연결 확인")
    p_test.add_argument("--channels", nargs="+", help="점검할 채널")

    return parser


def _print_status(cfg) -> None:
    """수집·분석 진행 상황을 한눈에."""
    from .store import Store

    with Store(cfg.db_path) as store:
        st = store.precedent_stats()
        bd = store.body_stats()
        taxes = store.tag_counts("tax", limit=6)

    print(f"  수집한 판례      : {st['total']:>5}건")
    print(f"    - 본문 있음    : {st['with_body']:>5}건  (분석 가능)")
    print(f"      · 국세청 보충: {bd['from_nts']:>5}건")
    print(f"    - 본문 없음    : {bd['bodiless']:>5}건  (보충 포기 {bd['gave_up']}건)")
    print(f"  분석 완료        : {st['analyzed']:>5}건")
    print(f"  분석 대기        : {st['pending']:>5}건")

    if taxes:
        print("\n  세목: " + ", ".join(f"{name} {n}" for name, n in taxes))

    if bd["bodiless"] - bd["gave_up"] > 0:
        print(
            f"\n  본문 없는 {bd['bodiless'] - bd['gave_up']}건은 "
            "`python -m taxrefund bodies --all` 로 보충할 수 있습니다."
        )

    if st["pending"]:
        per_run = cfg.analyze.per_run_limit
        days = -(-st["pending"] // max(per_run, 1))  # 올림
        print(f"\n  하루 {per_run}건씩이면 약 {days}일 걸립니다.")


def _make_output_safe() -> None:
    """콘솔이 이모지를 못 찍어도 죽지 않게 합니다.

    윈도우 기본 콘솔은 cp949라 '📱' 같은 문자를 인코딩하지 못하고
    UnicodeEncodeError로 서버 전체를 멈춰 세웁니다. 로그 한 줄 때문에
    상시 실행 프로세스가 죽는 건 손해가 너무 큽니다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)

    if args.command == "crawl":
        if args.since_days:
            cfg.crawl.since_days = args.since_days
        if args.max_pages:
            cfg.crawl.max_pages = args.max_pages
        years = cfg.crawl.since_days / 365
        print(f"\n판례 수집 — 최근 {cfg.crawl.since_days}일 ({years:.1f}년)")
        print("-" * 60)
        try:
            added = crawl(cfg)
        except LawApiError as exc:
            print(f"\n[오류] {exc}", file=sys.stderr)
            return 2
        _print_status(cfg)
        print(f"\n  이번에 새로 받은 건: {added}건")
        return 0

    if args.command == "bodies":
        from .pipeline import fetch_bodies

        limit = None if args.all else (args.limit or cfg.nts_per_run_limit)
        print("\n국세법령정보시스템 본문 보충")
        print("-" * 60)
        print(f"  대상: 본문이 비어 분석에 못 들어간 판례 ({limit or '전부'}건)")
        filled = fetch_bodies(cfg, limit=limit, verbose=True)
        print(f"\n  본문을 붙인 건: {filled}건")
        _print_status(cfg)
        return 0

    if args.command == "status":
        print("\n수집·분석 진행 상황")
        print("-" * 60)
        _print_status(cfg)
        return 0

    if args.command == "check":
        return 1 if check(cfg) else 0

    if args.command == "diagnose":
        return diagnose(cfg)

    if args.command == "headlines":
        print("\n한 줄 요지 보정")
        print("-" * 60)
        try:
            return headlines(cfg, limit=args.limit, force=args.force)
        except CredentialsError as exc:
            print(f"\n[오류] {exc}", file=sys.stderr)
            return 2

    if args.command == "companies":
        from .companies import build_index, find, show_stats
        from .dart import DartError

        print(f"\nDART 기업 색인 — {args.co_command}")
        print("-" * 60)
        try:
            if args.co_command == "build":
                return build_index(
                    cfg, limit=args.limit, refresh_filers=args.refresh_filers
                )
            if args.co_command == "stats":
                return show_stats(cfg)
            return find(
                cfg, args.ksic, limit=args.limit, audited_only=args.audited_only
            )
        except DartError as exc:
            print(f"\n[오류] {exc}", file=sys.stderr)
            return 2

    if args.command == "models":
        from .analyzer_gemini import list_models

        try:
            names = list_models(cfg.secrets.gemini_api_key)
        except CredentialsError as exc:
            print(f"\n[오류] {exc}", file=sys.stderr)
            print("발급: https://aistudio.google.com/apikey", file=sys.stderr)
            return 2
        current = cfg.analyze.resolved_model()
        print(f"\n사용 가능한 Gemini 모델 {len(names)}개 (현재 설정: {current})")
        print("-" * 60)
        for name in names:
            print(f"  {'>' if name == current else ' '} {name}")
        return 0

    if args.command == "list":
        with Store(cfg.db_path) as store:
            rows = store.recent(args.limit)
        if not rows:
            print("저장된 판례가 없습니다. 먼저 `run`을 실행하세요.")
            return 0
        print(f"{'점수':>4}  {'선고일':<12} {'법원':<10} {'사건번호':<16} 제목")
        print("-" * 100)
        for r in rows:
            score = r["confidence"]
            print(
                f"{(score if score is not None else '-'):>4}  "
                f"{(r['decided_on'] or '-'):<12} {(r['court'] or '-')[:10]:<10} "
                f"{(r['case_no'] or '-')[:16]:<16} {(r['title'] or '')[:50]}"
            )
        return 0

    if args.command == "web":
        if args.host:
            cfg.web.host = args.host
        if args.port:
            cfg.web.port = args.port
        print("\n📱 대시보드 실행 중 (Ctrl+C로 중지)")
        if not cfg.secrets.web_token:
            print("  ⚠ WEB_TOKEN이 없어 같은 네트워크의 누구나 접속할 수 있습니다.")
        try:
            serve_forever(cfg)
        except KeyboardInterrupt:
            print("\n중지합니다.")
        return 0

    if args.command == "backfill":
        print("\n밀린 알림 발송")
        print("-" * 60)
        pending = backfill(
            cfg, channels=args.channels, limit=args.limit, dry_run=args.dry_run
        )
        print("-" * 60)
        print(f"총 {sum(len(v) for v in pending.values())}건")
        return 0

    if args.command == "serve":
        if args.daily_at:
            cfg.serve.daily_at = args.daily_at
        if args.interval:
            cfg.serve.daily_at = None
            cfg.serve.interval_minutes = args.interval
        if args.no_run_on_start:
            cfg.serve.run_on_start = False
        if args.channels:
            cfg.notify.channels = args.channels

        setup_file_logging(cfg, args.verbose)
        server = Server(
            cfg,
            sample=args.sample,
            limit=args.limit,
            force=args.force,
            channels=args.channels,
        )
        try:
            return server.serve()
        except KeyboardInterrupt:
            print("\n중지합니다.")
            return 0

    if args.command == "test-notify":
        results = Notifier(cfg).send([_demo_alert()], args.channels)
        for channel, status in results.items():
            print(f"  {channel}: {STATUS_LABEL.get(status, status)}")
        # 미설정(건너뜀)은 실패가 아닙니다.
        return 0 if all(s != "failed" for s in results.values()) else 1

    # --- run ---------------------------------------------------------
    def once() -> int:
        try:
            alerts = run(
                cfg,
                sample=args.sample,
                limit=args.limit,
                force=args.force,
                dry_run=args.dry_run,
                channels=args.channels,
            )
        except CredentialsError as exc:
            print(f"\n[오류] {exc}\n", file=sys.stderr)
            return 2
        except LawApiError as exc:
            print(f"\n[오류] {exc}\n", file=sys.stderr)
            print("등록 전에는 `taxrefund run --sample`로 동작을 확인할 수 있습니다.", file=sys.stderr)
            return 2
        print(f"\n완료: 알림 대상 {len(alerts)}건")
        return 0

    if not args.every:
        return once()

    print(f"{args.every}분 간격으로 반복 실행합니다. 중지하려면 Ctrl+C.")
    while True:
        print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 실행 =====")
        try:
            once()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("실행 중 오류: %s", exc)
        try:
            time.sleep(args.every * 60)
        except KeyboardInterrupt:
            print("\n중지합니다.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
