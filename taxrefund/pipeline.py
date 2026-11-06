"""크롤링 -> 분석 -> 매칭 -> 알림 파이프라인."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .analyzer import CredentialsError, QuotaExceededError, make_analyzer
from .config import Config
from .discord_client import DiscordError, DiscordSender
from .lawgo import LawApiError, LawGoKrClient
from .matcher import build_alerts
from .models import Alert, Analysis, Precedent, _parse_kdate
from .netcheck import get_public_ip, registration_hint
from .notifier import SENT, STATUS_LABEL, Notifier
from .store import META_LAST_IP, META_LAST_RUN, META_LAW_OK_IP, Store

log = logging.getLogger(__name__)


def load_sample_precedents(root: Path) -> list[Precedent]:
    path = root / "data" / "sample_precedents.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Precedent(
            source=item["source"],
            doc_id=item["doc_id"],
            title=item["title"],
            case_no=item.get("case_no", ""),
            court=item.get("court", ""),
            decided_on=_parse_kdate(item.get("decided_on")),
            summary=item.get("summary", ""),
            body=item.get("body", ""),
            url=item.get("url", ""),
        )
        for item in raw
    ]


def iter_precedents(
    cfg: Config, sample: bool, skip_keys: set[str] | None = None
) -> Iterator[Precedent]:
    if sample:
        yield from load_sample_precedents(cfg.root)
        return

    client = LawGoKrClient(
        oc=cfg.secrets.law_api_oc or "",
        interval_sec=cfg.crawl.request_interval_sec,
    )
    yield from client.collect(
        targets=cfg.crawl.targets,
        queries=cfg.crawl.queries,
        since_days=cfg.crawl.since_days,
        max_per_query=cfg.crawl.max_per_query,
        max_body_chars=cfg.crawl.max_body_chars,
        skip_keys=skip_keys,
        max_pages=cfg.crawl.max_pages,
    )


def crawl(cfg: Config, *, sample: bool = False, store: Store | None = None) -> int:
    """판례를 받아 본문까지 저장만 합니다. 분석은 하지 않습니다.

    이미 본문을 받아 둔 건은 상세 조회를 건너뛰므로, 매일 돌려도
    새로 등재된 것과 아직 못 받은 과거 구간만 추가로 긁습니다.
    """
    owned = store is None
    store = store or Store(cfg.db_path)
    try:
        known = store.stored_keys()
        added = 0
        for prec in iter_precedents(cfg, sample, skip_keys=known):
            if prec.key not in known:
                added += 1
            store.save_precedent(prec)
        stats = store.precedent_stats()
        log.info(
            "수집: 신규 %d건 | 누적 %d건 (분석 완료 %d건, 대기 %d건)",
            added, stats["total"], stats["analyzed"], stats["pending"],
        )
        return added
    finally:
        if owned:
            store.close()


def fetch_bodies(
    cfg: Config,
    *,
    limit: int | None = None,
    store: Store | None = None,
    client=None,
    verbose: bool = False,
) -> int:
    """본문이 비어 있는 판례를 국세법령정보시스템에서 보충합니다.

    법령정보센터가 '국세법령정보시스템' 출처 건의 본문을 주지 않아서,
    수집만 되고 분석 큐에 못 들어간 건이 쌓입니다. 사건번호로 국세청을
    찾아 본문을 붙여 주면 그 건들이 정상적으로 분석 대상이 됩니다.

    국세청 쪽은 공식 API가 아니므로 실패해도 파이프라인을 세우지 않습니다.
    """
    from .nts import NtsClient  # 이 단계를 끄면 requests 세션도 안 만듭니다.

    owned = store is None
    store = store or Store(cfg.db_path)
    client = client or NtsClient(request_interval_sec=cfg.nts_interval_sec)
    filled = 0
    try:
        targets = store.bodiless_precedents(limit=limit)
        if not targets:
            log.info("본문을 보충할 판례가 없습니다.")
            return 0
        log.info("국세청 본문 보충: %d건 시도", len(targets))
        for prec in targets:
            if not prec.case_no:
                store.mark_body_missing(prec.key)
                continue
            try:
                doc = client.fetch_by_case_no(prec.case_no, prec.title)
            except Exception as exc:  # 화면이 바뀌면 어떤 예외든 날 수 있음
                log.warning("국세청 조회 중단 (%s): %s", prec.case_no, exc)
                store.mark_body_missing(prec.key)
                continue

            # 본문이 너무 짧으면 붙여 봐야 분석이 못 합니다. 심리불속행
            # 기각처럼 원래 짧은 것도 있어서 버리지는 않고, 요지가 있으면
            # 요약으로라도 남겨 분석 큐에는 올립니다.
            body = (doc.body if doc else "")[: cfg.crawl.max_body_chars]
            if doc and (body or doc.gist):
                store.attach_body(
                    prec.key,
                    body,
                    source="nts",
                    tax_type=doc.tax_type,
                    summary=doc.gist,
                )
                filled += 1
                if verbose:
                    log.info("  + %s %s (%d자)", prec.case_no, doc.tax_type, len(body))
            else:
                store.mark_body_missing(prec.key)
                if verbose:
                    log.info("  - %s 본문 없음", prec.case_no)
        stats = store.body_stats()
        log.info(
            "본문 보충 %d건 | 국세청 출처 누적 %d건 | 아직 본문 없음 %d건 (포기 %d건)",
            filled, stats["from_nts"], stats["bodiless"], stats["gave_up"],
        )
        return filled
    finally:
        if owned:
            store.close()


def run(
    cfg: Config,
    *,
    sample: bool = False,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    channels: list[str] | None = None,
) -> list[Alert]:
    """전체 파이프라인을 한 번 돌립니다."""
    analyzer = make_analyzer(cfg.analyze, cfg.secrets)
    pairs: list[tuple[Precedent, Analysis]] = []

    with Store(cfg.db_path) as store:
        # 1단계: 수집. 모델 한도와 무관하게 끝까지 받아 둡니다.
        crawl(cfg, sample=sample, store=store)

        # 1.5단계: 본문 보충. 법령정보센터가 본문을 안 주는 건을 국세청에서
        # 채웁니다. 이걸 먼저 해야 그 건들이 이번 회차 분석 대상에 들어갑니다.
        if cfg.nts_enabled and not sample:
            try:
                fetch_bodies(cfg, limit=cfg.nts_per_run_limit, store=store)
            except Exception as exc:
                # 공식 API가 아니라 언제든 깨질 수 있습니다. 여기서 파이프라인
                # 전체를 세우면 수집·분석·알림까지 같이 멈춥니다.
                log.warning("국세청 본문 보충을 건너뜁니다: %s", exc)

        # 2단계: 분석. 밀린 것 중 최신순으로 한도만큼만 소화합니다.
        if limit is not None:
            budget = limit
        else:
            budget = None if force else cfg.analyze.per_run_limit

        # 제목이 빈 건을 먼저 채웁니다. 요약만 넣어 요청이 가볍고, 목록에서
        # 서로 구분되지 않는 문제가 눈에 가장 먼저 띄기 때문입니다. 다만
        # 분석을 굶기지 않도록 이번 예산의 절반까지만 씁니다.
        if budget and not force:
            generate = getattr(analyzer, "generate_headline", None)
            missing = [
                (p, a) for p, a in store.analyzed_pairs()
                if not (a.headline or "").strip()
            ]
            if generate and missing:
                before = _api_calls(analyzer)
                used = fill_headlines(
                    store, generate, missing,
                    analyzer=analyzer, max_calls=max(1, budget // 2),
                )
                spent = _api_calls(analyzer) - before
                log.info(
                    "한 줄 요지 %d건 보정 (요청 %d회, 남은 %d건)",
                    used, spent, len(missing) - used,
                )
                budget = max(0, budget - max(spent, used))
        backlog = (
            store.analyzable_precedents(limit=budget)  # 이미 분석한 것도 다시
            if force
            else store.unanalyzed_precedents(limit=budget)
        )
        pending = store.precedent_stats()["pending"]
        if pending:
            log.info("분석 대기 %d건 중 이번에 %d건을 봅니다.", pending, len(backlog))

        analysis_start = _api_calls(analyzer)
        for prec in backlog:
            if budget is not None and _api_calls(analyzer) - analysis_start >= budget:
                log.info("분석: 배정된 요청 %d회를 다 썼습니다. 나머지는 다음 실행에서.", budget)
                break
            log.info("분석 중: [%s] %s", prec.decided_str, prec.title[:60])
            try:
                analysis = analyzer.analyze(prec)
            except QuotaExceededError as exc:
                # 무료 등급 한도 소진. 여기까지 분석한 건은 살리고 중단합니다.
                log.warning("%s 지금까지 분석한 %d건으로 진행합니다.", exc, len(pairs))
                break
            if analysis is None:
                store.mark_analysis_failed(prec.key)  # 매일 같은 건에 걸리지 않게
                continue
            store.save_analysis(prec.key, analysis)
            pairs.append((prec, analysis))

        # 같은 판결이 두 출처에서 들어와 있으면 알림이 두 번 갑니다.
        dup_secondary = store.duplicate_secondaries()
        alerts = build_alerts(
            [(p, a) for p, a in pairs if p.key not in dup_secondary],
            cfg.watchlist, cfg.notify,
        )
        log.info("분석 %d건 중 알림 대상 %d건", len(pairs), len(alerts))

        store.set_meta(META_LAST_RUN, datetime.now().isoformat(timespec="seconds"))
        if not sample:
            # 여기까지 왔다는 건 법령 API 호출이 통과했다는 뜻입니다.
            # 그때의 공인 IP를 기억해 두면 이후 IP 변동을 감지할 수 있습니다.
            current_ip = get_public_ip()
            if current_ip:
                store.set_meta(META_LAST_IP, current_ip)
                store.set_meta(META_LAW_OK_IP, current_ip)

        if dry_run:
            print(f"\n[dry-run] 알림 대상 {len(alerts)}건 (발송하지 않음)")
            for a in alerts:
                companies = ", ".join(m.company for m in a.matches)
                print(f"  - [{a.score}점] {a.precedent.title[:50]} -> {companies}")
            return alerts

        # 이미 보낸 건은 다시 보내지 않습니다.
        target_channels = channels or cfg.notify.channels
        fresh: dict[str, list[Alert]] = {}
        for channel in target_channels:
            fresh[channel] = [
                a for a in alerts if force or not store.was_notified(a.precedent.key, channel)
            ]

        notifier = Notifier(cfg)
        for channel, items in fresh.items():
            if not items:
                log.info("[%s] 새로 보낼 알림 없음", channel)
                continue
            result = notifier.send(items, [channel])
            # 실제로 발송된 경우에만 기록합니다. 미설정으로 건너뛴 채널은
            # 기록을 남기지 않아, 나중에 backfill로 그대로 받을 수 있습니다.
            if result.get(channel) == SENT:
                for a in items:
                    store.mark_notified(a.precedent.key, channel)

    return alerts


def backfill(
    cfg: Config,
    *,
    channels: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, list[Alert]]:
    """이미 분석해 둔 판례 중 아직 그 채널로 보내지 않은 알림을 보냅니다.

    디스코드 봇을 나중에 붙였을 때, 그동안 쌓인 알림을 받기 위한 명령입니다.
    """
    channels = channels or cfg.notify.channels
    notifier = Notifier(cfg)
    pending: dict[str, list[Alert]] = {}

    with Store(cfg.db_path) as store:
        pairs = store.analyzed_pairs(dedupe=True)
        alerts = build_alerts(pairs, cfg.watchlist, cfg.notify)
        log.info("저장된 분석 %d건 중 알림 기준 통과 %d건", len(pairs), len(alerts))

        for channel in channels:
            items = [a for a in alerts if not store.was_notified(a.precedent.key, channel)]
            if limit is not None:
                items = items[:limit]
            pending[channel] = items

            if not items:
                print(f"  {channel}: 밀린 알림 없음")
                continue
            if dry_run:
                print(f"  {channel}: 밀린 알림 {len(items)}건 (발송하지 않음)")
                for a in items:
                    companies = ", ".join(m.company for m in a.matches)
                    print(f"    - [{a.score}점] {a.precedent.title[:50]} -> {companies}")
                continue

            result = notifier.send(items, [channel])
            status = result.get(channel)
            if status == SENT:
                for a in items:
                    store.mark_notified(a.precedent.key, channel)
                print(f"  {channel}: {len(items)}건 발송 완료")
            else:
                print(f"  {channel}: {STATUS_LABEL.get(status, status)} — 발송되지 않았습니다")

    return pending


# 봇 초대 URL 권한값: Send Messages(1<<11) + Embed Links(1<<14)
DISCORD_INVITE_PERMISSIONS = (1 << 11) | (1 << 14)


def _print_discord_setup_guide() -> None:
    print("         1) https://discord.com/developers/applications -> New Application")
    print("         2) Bot 탭 -> Reset Token -> .env의 DISCORD_BOT_TOKEN")
    print("         3) 아래 URL의 <APP_ID>를 General Information의 Application ID로 바꿔 열고")
    print("            봇을 서버에 초대:")
    print(
        f"            https://discord.com/oauth2/authorize?client_id=<APP_ID>"
        f"&scope=bot&permissions={DISCORD_INVITE_PERMISSIONS}"
    )
    print("         4) 디스코드 설정 > 고급 > 개발자 모드 ON -> 채널 우클릭 > 채널 ID 복사")
    print("            -> .env의 DISCORD_CHANNEL_ID")
    print("         설정 후: python -m taxrefund check && python -m taxrefund backfill")


def headlines(cfg: Config, *, limit: int | None = None, force: bool = False) -> int:
    """headline이 비어 있는 기존 분석에 한 줄 요지를 채웁니다.

    스키마에 headline이 추가되기 전에 분석한 건들을 위한 보정입니다.
    판례 본문 전체가 아니라 저장된 요약만 넣으므로 요청이 가볍습니다.
    """
    analyzer = make_analyzer(cfg.analyze, cfg.secrets)
    generate = getattr(analyzer, "generate_headline", None)
    if generate is None:
        print(f"  {cfg.analyze.provider} provider는 headline 보정을 지원하지 않습니다.")
        return 1

    with Store(cfg.db_path) as store:
        targets = [
            (prec, analysis)
            for prec, analysis in store.analyzed_pairs()
            if force or not (analysis.headline or "").strip()
        ]
        if limit is not None:
            targets = targets[:limit]

        if not targets:
            print("  보정할 건이 없습니다. 모두 한 줄 요지를 가지고 있습니다.")
            return 0

        print(f"  {len(targets)}건 보정 시작")
        done = fill_headlines(store, generate, targets, verbose=True)
        print(f"  {done}건 보정 완료")
    return 0


def _api_calls(analyzer) -> int:
    """지금까지 쓴 실제 요청 수. 카운터가 없는 분석기는 0으로 봅니다."""
    return getattr(analyzer, "api_calls", 0)


def fill_headlines(store, generate, targets, *, verbose: bool = False,
                   analyzer=None, max_calls: int | None = None) -> int:
    """저장된 분석에 한 줄 요지를 채웁니다. -> 실제로 채운 건수

    모델 한도가 떨어지면 거기까지 채운 것을 남기고 멈춥니다. 판례 본문을
    다시 넣지 않고 저장된 요약만 쓰므로 분석보다 훨씬 가볍습니다.

    max_calls는 '건수'가 아니라 '요청 수' 상한입니다. 503 재시도도 한도를
    먹으므로 건수로 막으면 뒤따르는 분석이 굶습니다.
    """
    start = _api_calls(analyzer)
    done = 0
    for prec, analysis in targets:
        if max_calls is not None and _api_calls(analyzer) - start >= max_calls:
            log.info("한 줄 요지: 배정된 요청 %d회를 다 썼습니다.", max_calls)
            break
        try:
            headline = generate(prec, analysis)
        except QuotaExceededError as exc:
            if verbose:
                print(f"  {exc}")
            else:
                log.info("한 줄 요지 보정 중단: %s", exc)
            break
        if not headline:
            if verbose:
                print(f"  - 실패: {prec.title[:40]}")
            continue
        analysis.headline = headline
        store.save_analysis(prec.key, analysis)
        done += 1
        if verbose:
            print(f"  - {headline}")
    return done


def diagnose(cfg: Config) -> int:
    """법령 OPEN API가 거부될 때 원인을 좁혀 줍니다.

    이 API는 'OC를 모르는 경우'와 'IP가 등록되지 않은 경우'에 **똑같은**
    오류를 돌려주므로, 응답만으로는 구분할 수 없습니다. 그래서 확인해야 할
    항목을 체크리스트로 뽑아 줍니다.
    """
    oc = cfg.secrets.law_api_oc
    current_ip = get_public_ip(force=True)

    print("\n법령정보 OPEN API 진단")
    print("=" * 62)
    print(f"  현재 공인 IP : {current_ip or '확인 실패'}")
    print(f"  LAW_API_OC   : {oc or '(비어 있음)'}")

    registered_ip = None
    if cfg.db_path.exists():
        try:
            with Store(cfg.db_path) as store:
                registered_ip = store.get_meta(META_LAW_OK_IP)
        except Exception:
            pass
    if registered_ip:
        mark = "일치" if registered_ip == current_ip else "★ 달라짐"
        print(f"  마지막 성공 IP: {registered_ip}  ({mark})")

    if not oc:
        print("\n  LAW_API_OC가 비어 있습니다. .env에 먼저 채우세요.")
        return 1

    print("\n  대상별 호출 결과")
    print("  " + "-" * 60)
    client = LawGoKrClient(oc, cfg.crawl.request_interval_sec)
    results: dict[str, str] = {}
    for target, label in (("law", "법령"), ("prec", "판례"), ("expc", "법령해석례")):
        try:
            records = client.search(target, "법인세", display=1)
            results[target] = "ok"
            print(f"    {label:10s}(target={target:5s}) : 성공 — {len(records)}건")
        except LawApiError as exc:
            results[target] = "denied"
            print(f"    {label:10s}(target={target:5s}) : 거부 — {str(exc).splitlines()[0][:44]}")
        except Exception as exc:
            results[target] = "error"
            print(f"    {label:10s}(target={target:5s}) : 오류 — {type(exc).__name__}")

    print("  " + "-" * 60)

    if all(v == "ok" for v in results.values()):
        print("\n  정상입니다. 서버가 다음 사이클에 수집을 시작합니다.")
        if current_ip:
            with Store(cfg.db_path) as store:
                store.set_meta(META_LAST_IP, current_ip)
                store.set_meta(META_LAW_OK_IP, current_ip)
        return 0

    if any(v == "ok" for v in results.values()):
        denied = [t for t, v in results.items() if v != "ok"]
        print(f"\n  일부 대상만 거부됩니다: {', '.join(denied)}")
        print("  -> 활용 신청 시 해당 대상을 포함하지 않았을 수 있습니다.")
        print("     open.law.go.kr 마이페이지에서 신청 대상에 '판례', '법령해석례'를 추가하세요.")
        return 1

    # 전부 거부: 응답만으로는 원인을 알 수 없으므로 체크리스트를 제시합니다.
    print("\n  모든 대상이 거부되었습니다. 이 API는 'OC 불일치'와 'IP 미등록'에")
    print("  동일한 오류를 반환하므로, 아래를 순서대로 확인해야 합니다.")
    print("\n  open.law.go.kr 로그인 후 [마이페이지 > OPEN API 신청현황]에서 확인:")
    print(f"    1) 신청 상태가 '승인'인지 (승인 대기 중이면 반영될 때까지 거부됩니다)")
    print(f"    2) 등록된 IP가 정확히  {current_ip or '(현재 IP 확인 실패)'}  인지")
    print(f"       - 공유기 내부 IP(192.168.x.x)를 등록했다면 잘못된 것입니다")
    print(f"    3) 사용자 ID(OC)가 정확히  {oc}  인지")
    print(f"       - 신청에 쓴 이메일의 @ 앞부분입니다. 다른 이메일로 가입했다면 그 값을 쓰세요")
    print("    4) 신청 대상에 '판례'와 '법령해석례'가 포함되어 있는지")
    print("\n  값을 고친 뒤 다시: python -m taxrefund diagnose")
    return 1


def check(cfg: Config) -> int:
    """설정이 제대로 되었는지 점검합니다. 문제 개수를 반환."""
    problems = 0

    def ok(label: str, detail: str = "") -> None:
        print(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))

    def pending(label: str, detail: str) -> None:
        """나중에 설정할 예정인 항목. 오류로 세지 않습니다."""
        print(f"  [대기] {label} — {detail}")

    def bad(label: str, detail: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  [FAIL] {label} — {detail}")

    print("\n설정 점검")
    print("-" * 60)

    provider = (cfg.analyze.provider or "gemini").lower()
    model = cfg.analyze.resolved_model()
    if provider == "gemini":
        if cfg.secrets.gemini_api_key:
            ok("분석 모델", f"gemini · {model}")
            try:
                from .analyzer_gemini import verify_model

                # 목록에 있어도 실제로는 못 쓰는 모델이 있어서 한 번 호출해 봅니다.
                verify_model(cfg.secrets.gemini_api_key, model)
                ok("Gemini 호출", "실제 응답 확인됨")
            except CredentialsError as exc:
                bad("Gemini 인증", str(exc))
            except Exception as exc:
                detail = str(exc).replace("\n", " ")
                bad("Gemini 호출", detail[:150])
                print("         `python -m taxrefund models`로 쓸 수 있는 모델을 확인하세요.")
        else:
            bad(
                "GEMINI_API_KEY",
                ".env에 없음 — https://aistudio.google.com/apikey 에서 무료 발급",
            )
    else:
        if cfg.secrets.anthropic_api_key:
            ok("분석 모델", f"claude · {model}")
        else:
            print("  [ ? ]  ANTHROPIC_API_KEY 미설정 — SDK가 환경변수/ant 프로필에서 찾습니다")

    current_ip = get_public_ip()
    registered_ip = None
    if cfg.db_path.exists():
        try:
            with Store(cfg.db_path) as store:
                registered_ip = store.get_meta(META_LAW_OK_IP)
        except Exception:
            pass

    if current_ip and registered_ip and current_ip != registered_ip:
        bad("공인 IP 변동", f"{registered_ip} → {current_ip} (법령 API 재등록 필요)")
    else:
        ok("공인 IP", current_ip or "확인 실패")

    if cfg.secrets.law_api_oc:
        ok("LAW_API_OC", cfg.secrets.law_api_oc)
        try:
            client = LawGoKrClient(cfg.secrets.law_api_oc, cfg.crawl.request_interval_sec)
            records = client.search(cfg.crawl.targets[0], cfg.crawl.queries[0], display=1)
            ok("법령정보 OPEN API 연결", f"{len(records)}건 응답")
            if current_ip:
                with Store(cfg.db_path) as store:
                    store.set_meta(META_LAST_IP, current_ip)
                    store.set_meta(META_LAW_OK_IP, current_ip)
        except LawApiError as exc:
            bad("법령정보 OPEN API", str(exc).splitlines()[0])
            for line in registration_hint(current_ip, registered_ip).replace("**", "").splitlines():
                print(f"         {line}")
        except Exception as exc:
            bad("법령정보 OPEN API", f"{type(exc).__name__}: {exc}")
    else:
        bad("LAW_API_OC", ".env에 설정되지 않음 (--sample 모드만 사용 가능)")

    if cfg.watchlist:
        ok("워치리스트", f"{len(cfg.watchlist)}개 회사: " + ", ".join(c.name for c in cfg.watchlist))
    else:
        bad("워치리스트", "config.yaml의 watchlist가 비어 있음")

    for channel in cfg.notify.channels:
        if channel == "slack" and not cfg.secrets.slack_webhook_url:
            bad("slack 채널", "SLACK_WEBHOOK_URL 미설정")
        elif channel == "email" and not (cfg.secrets.smtp_host and cfg.secrets.smtp_to):
            bad("email 채널", "SMTP_HOST / SMTP_TO 미설정")
        elif channel == "discord":
            if not cfg.discord_ready:
                # 아직 봇을 안 만들었을 뿐이라 오류로 세지 않습니다.
                missing = cfg.discord_partial
                if missing:
                    bad("discord 채널", f"{missing}가 비어 있습니다 (봇 설정이 절반만 되어 있음)")
                else:
                    pending("discord 채널", "아직 미설정 — 봇을 만들면 아래 절차대로 채우세요")
                    _print_discord_setup_guide()
            else:
                try:
                    sender = DiscordSender(
                        bot_token=cfg.secrets.discord_bot_token,
                        channel_id=cfg.secrets.discord_channel_id,
                        webhook_url=cfg.secrets.discord_webhook_url,
                    )
                    ok("discord 채널", f"{sender.mode} 모드 · {sender.verify()}")
                except DiscordError as exc:
                    bad("discord 채널", str(exc))
                except Exception as exc:
                    bad("discord 채널", f"{type(exc).__name__}: {exc}")
        else:
            ok(f"{channel} 채널")

    schedule = (
        f"매일 {cfg.serve.daily_at}"
        if cfg.serve.daily_at
        else f"{cfg.serve.interval_minutes}분마다"
    )
    ok("서버 일정", schedule)

    # 채널별로 아직 보내지 못하고 쌓인 알림
    try:
        if cfg.db_path.exists():
            with Store(cfg.db_path) as store:
                stored = store.analyzed_pairs(dedupe=True)
                if stored:
                    alerts = build_alerts(stored, cfg.watchlist, cfg.notify)
                    for channel in cfg.notify.channels:
                        waiting = sum(
                            1 for a in alerts if not store.was_notified(a.precedent.key, channel)
                        )
                        if waiting:
                            pending(
                                f"{channel} 미발송",
                                f"{waiting}건 대기 중 — `python -m taxrefund backfill`로 발송",
                            )
    except Exception as exc:
        log.debug("미발송 집계 실패: %s", exc)

    print("-" * 60)
    print("문제 없음" if problems == 0 else f"문제 {problems}건")
    return problems
