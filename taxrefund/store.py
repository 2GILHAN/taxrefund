"""이미 처리한 판례를 기억해서 중복 알림을 막습니다 (SQLite)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Analysis, Precedent, _parse_kdate

# 이만큼 실패하면 그 판례는 분석 큐에서 뺍니다.
MAX_ANALYSIS_ATTEMPTS = 3

# 국세청에서 본문을 이만큼 찾아보고도 못 찾으면 더 두드리지 않습니다.
MAX_BODY_TRIES = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS precedents (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    title       TEXT,
    case_no     TEXT,
    court       TEXT,
    decided_on  TEXT,
    url         TEXT,
    -- 본문을 함께 보관해야 '수집'과 '분석'을 분리할 수 있습니다.
    -- 모델 한도로 오늘 못 본 건을 내일 다시 크롤링하지 않아도 됩니다.
    summary     TEXT DEFAULT '',
    body        TEXT DEFAULT '',
    -- 분석 실패 횟수. 계속 실패하는 건이 매일 한도를 태우지 않게 합니다.
    attempts    INTEGER DEFAULT 0,
    -- 세목(법인세/부가가치세/...). 국세법령정보시스템이 붙여 주는 값.
    tax_type    TEXT DEFAULT '',
    -- 본문 출처. '' 법령정보센터 / 'nts' 국세법령정보시스템.
    body_source TEXT DEFAULT '',
    -- 국세청 본문을 찾아본 횟수. 검색해도 안 나오는 건이 매일 다시
    -- 국세청을 두드리지 않게 합니다.
    body_tries  INTEGER DEFAULT 0,
    fetched_at  TEXT NOT NULL
);


-- 태그 색인. 분석 payload(JSON) 안을 매번 훑어 필터링하면 판례가 늘수록
-- 대시보드가 느려지고, 태그 목록을 만들 수도 없습니다. 분석을 저장할 때
-- 여기로 펼쳐 둡니다. analyses에서 파생되는 표라 언제든 다시 만들 수
-- 있습니다(rebuild_tags).
CREATE TABLE IF NOT EXISTS tags (
    key     TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- tax | industry | issue | outcome | basis
    value   TEXT NOT NULL,
    PRIMARY KEY (key, kind, value)
);


CREATE TABLE IF NOT EXISTS analyses (
    key         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    confidence  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    key         TEXT NOT NULL,
    channel     TEXT NOT NULL,
    company     TEXT,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (key, channel, company)
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- DART 기업 색인. 업종코드로 조회하기 위해 미리 긁어 둡니다.
CREATE TABLE IF NOT EXISTS companies (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    corp_cls    TEXT,            -- Y 유가 / K 코스닥 / N 코넥스 / E 기타(외감)
    stock_code  TEXT,
    bizr_no     TEXT,
    jurir_no    TEXT,
    induty_code TEXT,            -- 한국표준산업분류
    adres       TEXT,
    est_dt      TEXT,
    ceo_nm      TEXT,
    acc_mt      TEXT,            -- 결산월. 경정청구 기한 계산에 씁니다
    updated_at  TEXT NOT NULL
);

"""

# 색인은 컬럼이 다 붙은 뒤에 만들어야 합니다. CREATE TABLE IF NOT EXISTS는
# 이미 있는 표에 컬럼을 못 붙이므로, 옛 DB에서는 뒤늦게 추가된 컬럼을
# 가리키는 CREATE INDEX가 "no such column"으로 터집니다.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_precedents_decided ON precedents(decided_on DESC);
CREATE INDEX IF NOT EXISTS idx_precedents_tax ON precedents(tax_type);
CREATE INDEX IF NOT EXISTS idx_tags_kind_value ON tags(kind, value);
CREATE INDEX IF NOT EXISTS idx_tags_key ON tags(key);
CREATE INDEX IF NOT EXISTS idx_companies_induty ON companies(induty_code);
CREATE INDEX IF NOT EXISTS idx_companies_name   ON companies(corp_name);
"""

# meta 테이블에서 쓰는 키
META_LAST_RUN = "last_run_at"
META_LAST_IP = "public_ip"
META_LAW_OK_IP = "law_api_ok_ip"  # 법령 API가 마지막으로 성공했을 때의 IP
META_DART_FILERS = "dart_filers"  # 사업보고서 제출 기업 고유번호 목록 (색인 대상)


def _row_to_precedent(row) -> Precedent:
    """precedents 행을 Precedent로 되돌립니다."""
    raw = row["decided_on"] or ""
    decided = None
    if raw and raw != "-":
        try:
            decided = date.fromisoformat(raw)
        except ValueError:
            decided = None
    keys = row.keys()
    return Precedent(
        source=row["source"],
        doc_id=row["doc_id"],
        title=row["title"] or "",
        case_no=row["case_no"] or "",
        court=row["court"] or "",
        decided_on=decided,
        summary=(row["summary"] if "summary" in keys else "") or "",
        body=(row["body"] if "body" in keys else "") or "",
        url=row["url"] or "",
        tax_type=(row["tax_type"] if "tax_type" in keys else "") or "",
        body_source=(row["body_source"] if "body_source" in keys else "") or "",
    )


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()          # 옛 DB에 빠진 컬럼을 먼저 채우고
        self.conn.executescript(INDEXES)   # 그 다음에 색인
        self.conn.commit()

    def _migrate(self) -> None:
        """기존 DB에 뒤늦게 추가된 컬럼을 채웁니다.

        CREATE TABLE IF NOT EXISTS는 이미 있는 표에 컬럼을 붙이지 못해서,
        먼저 만들어진 DB는 조용히 옛 구조로 남습니다.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(precedents)")}
        for column, ddl in (
            ("summary", "TEXT DEFAULT ''"),
            ("body", "TEXT DEFAULT ''"),
            ("attempts", "INTEGER DEFAULT 0"),
            ("tax_type", "TEXT DEFAULT ''"),
            ("body_source", "TEXT DEFAULT ''"),
            ("body_tries", "INTEGER DEFAULT 0"),
        ):
            if column not in have:
                self.conn.execute(
                    f"ALTER TABLE precedents ADD COLUMN {column} {ddl}"
                )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 판례 -----------------------------------------------------------
    def is_seen(self, key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM precedents WHERE key = ?", (key,)).fetchone()
        return row is not None

    def save_precedent(self, prec: Precedent) -> None:
        # attempts / body_tries 는 이 표의 것이 진실입니다. Precedent가 들고
        # 다니지 않는 값이라, REPLACE로 덮어쓰면 0으로 리셋되면서 계속
        # 실패하는 건이 매일 다시 큐에 들어옵니다.
        self.conn.execute(
            """INSERT OR REPLACE INTO precedents
               (key, source, doc_id, title, case_no, court, decided_on, url,
                summary, body, tax_type, body_source, attempts, body_tries, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,
                       COALESCE((SELECT attempts   FROM precedents WHERE key = ?), 0),
                       COALESCE((SELECT body_tries FROM precedents WHERE key = ?), 0), ?)""",
            (
                prec.key,
                prec.source,
                prec.doc_id,
                prec.title,
                prec.case_no,
                prec.court,
                prec.decided_str,
                prec.url,
                prec.summary,
                prec.body,
                prec.tax_type or prec.resolved_tax_type,
                prec.body_source,
                prec.key,
                prec.key,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    # -- 국세청 본문 보충 -------------------------------------------------
    def bodiless_precedents(
        self, limit: int | None = None, max_tries: int = 3
    ) -> list[Precedent]:
        """본문이 없어 분석 큐에 못 들어가는 판례를 최신순으로.

        국세청에서 본문을 보충할 대상입니다. 검색해도 안 나오는 건이
        있어서, 몇 번 찾아본 건은 빼 둡니다.
        """
        sql = (
            "SELECT * FROM precedents p "
            "WHERE COALESCE(p.body,'') = '' AND COALESCE(p.summary,'') = '' "
            f"  AND COALESCE(p.body_tries,0) < {int(max_tries)} "
            "ORDER BY COALESCE(p.decided_on,'') DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_row_to_precedent(r) for r in self.conn.execute(sql)]

    def attach_body(
        self, key: str, body: str, *, source: str, tax_type: str = "", summary: str = ""
    ) -> None:
        """뒤늦게 찾아낸 본문을 붙입니다.

        수집 당시의 제목·사건번호는 법령정보센터 것을 그대로 둡니다.
        본문만 다른 곳에서 왔다는 사실은 body_source로 남습니다.
        """
        self.conn.execute(
            "UPDATE precedents SET body = ?, body_source = ?, "
            "  tax_type = CASE WHEN ? != '' THEN ? ELSE tax_type END, "
            "  summary  = CASE WHEN ? != '' THEN ? ELSE summary  END, "
            "  body_tries = COALESCE(body_tries,0) + 1 "
            "WHERE key = ?",
            (body, source, tax_type, tax_type, summary, summary, key),
        )
        self.conn.commit()

    def mark_body_missing(self, key: str) -> None:
        """국세청에서도 본문을 찾지 못했다는 기록."""
        self.conn.execute(
            "UPDATE precedents SET body_tries = COALESCE(body_tries,0) + 1 WHERE key = ?",
            (key,),
        )
        self.conn.commit()

    # -- 같은 판결의 중복 ------------------------------------------------
    def duplicate_groups(self) -> dict[str, list[str]]:
        """같은 사건번호를 가진 판례 키 묶음 (2건 이상인 것만).

        법령정보센터와 국세법령정보시스템이 같은 판결을 각자 다른 일련번호로
        내놓습니다. 그대로 두면 한 판결이 목록에 두 번 뜨고 알림도 두 번
        갑니다. 사건번호가 겹치면 같은 판결로 봅니다.
        """
        from .nts import case_keys  # 사건번호 정규화 규칙을 한 곳에서만 둡니다.

        buckets: dict[str, list[sqlite3.Row]] = {}
        rows = self.conn.execute(
            "SELECT key, case_no, decided_on, LENGTH(COALESCE(body,'')) AS blen, "
            "       LENGTH(COALESCE(summary,'')) AS slen "
            "FROM precedents WHERE COALESCE(case_no,'') != ''"
        ).fetchall()
        for row in rows:
            for token in case_keys(row["case_no"]):
                buckets.setdefault(token, []).append(row)

        groups: dict[str, list[str]] = {}
        for token, found in buckets.items():
            if len(found) < 2:
                continue
            # 선고일까지 같아야 같은 판결로 봅니다. 사건번호 표기가 우연히
            # 겹치는 경우(다른 법원의 같은 번호)를 걸러 냅니다.
            by_date: dict[str, list[sqlite3.Row]] = {}
            for row in found:
                by_date.setdefault(row["decided_on"] or "", []).append(row)
            for same in by_date.values():
                if len(same) < 2:
                    continue
                # 본문이 긴 쪽을 대표로 둡니다. 분석에 쓸 정보가 더 많습니다.
                ordered = sorted(
                    same, key=lambda r: (r["blen"] + r["slen"], r["key"]), reverse=True
                )
                groups[token] = [r["key"] for r in ordered]
        return groups

    def duplicate_secondaries(self) -> set[str]:
        """중복 묶음에서 대표가 아닌 키. 목록·알림에서 빼는 데 씁니다."""
        return {k for keys in self.duplicate_groups().values() for k in keys[1:]}

    def body_stats(self) -> dict[str, int]:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "from_nts": one("SELECT COUNT(*) FROM precedents WHERE body_source = 'nts'"),
            "bodiless": one(
                "SELECT COUNT(*) FROM precedents "
                "WHERE COALESCE(body,'') = '' AND COALESCE(summary,'') = ''"
            ),
            "gave_up": one(
                "SELECT COUNT(*) FROM precedents "
                "WHERE COALESCE(body,'') = '' AND COALESCE(summary,'') = '' "
                f"  AND COALESCE(body_tries,0) >= {MAX_BODY_TRIES}"
            ),
        }

    # -- 분석 -----------------------------------------------------------
    def stored_keys(self, recheck_days: int = 30) -> set[str]:
        """재크롤링 때 상세 조회를 건너뛸 판례 키.

        본문을 받은 건은 영구히 건너뜁니다. 본문이 없던 건(국세법령정보시스템
        출처)은 매번 다시 긁으면 하루 수백 건이 헛돌기 때문에 일정 기간만
        건너뛰고, 그 뒤에는 본문이 붙었는지 다시 확인합니다.
        """
        stale = (datetime.now() - timedelta(days=recheck_days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT key FROM precedents "
            "WHERE COALESCE(body,'') != '' OR COALESCE(summary,'') != '' "
            "   OR fetched_at > ?",
            (stale,),
        )
        return {r["key"] for r in rows}

    def unanalyzed_precedents(self, limit: int | None = None) -> list[Precedent]:
        """아직 분석하지 않은 판례를 최신순으로. 본문이 있는 것만 돌려줍니다.

        수집은 하루에 다 끝나도 분석은 모델 한도에 묶이므로, 밀린 물량을
        여기서 꺼내 조금씩 소화합니다.
        """
        sql = (
            "SELECT * FROM precedents p "
            "WHERE NOT EXISTS (SELECT 1 FROM analyses a WHERE a.key = p.key) "
            "  AND (COALESCE(p.body,'') != '' OR COALESCE(p.summary,'') != '') "
            f"  AND COALESCE(p.attempts,0) < {MAX_ANALYSIS_ATTEMPTS} "
            "ORDER BY COALESCE(p.decided_on,'') DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_row_to_precedent(r) for r in self.conn.execute(sql)]

    def analyzable_precedents(self, limit: int | None = None) -> list[Precedent]:
        """본문이 있는 판례 전부. --force로 재분석할 때 씁니다."""
        sql = (
            "SELECT * FROM precedents p "
            "WHERE COALESCE(p.body,'') != '' OR COALESCE(p.summary,'') != '' "
            "ORDER BY COALESCE(p.decided_on,'') DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_row_to_precedent(r) for r in self.conn.execute(sql)]

    def mark_analysis_failed(self, key: str) -> None:
        """분석이 실패한 건의 시도 횟수를 올립니다.

        본문이 이상하거나 모델이 계속 JSON을 깨뜨리는 건이 있습니다.
        기록해 두지 않으면 그 한 건이 매일 한도를 먼저 먹습니다.
        """
        self.conn.execute(
            "UPDATE precedents SET attempts = COALESCE(attempts,0) + 1 WHERE key = ?",
            (key,),
        )
        self.conn.commit()

    def precedent_stats(self) -> dict[str, int]:
        """수집·분석 진행 상황."""
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "total": one("SELECT COUNT(*) FROM precedents"),
            "with_body": one(
                "SELECT COUNT(*) FROM precedents "
                "WHERE COALESCE(body,'') != '' OR COALESCE(summary,'') != ''"
            ),
            "analyzed": one("SELECT COUNT(*) FROM analyses"),
            "pending": one(
                "SELECT COUNT(*) FROM precedents p "
                "WHERE NOT EXISTS (SELECT 1 FROM analyses a WHERE a.key = p.key) "
                "  AND (COALESCE(p.body,'') != '' OR COALESCE(p.summary,'') != '') "
                f"  AND COALESCE(p.attempts,0) < {MAX_ANALYSIS_ATTEMPTS}"
            ),
        }

    def get_analysis(self, key: str) -> Analysis | None:
        row = self.conn.execute("SELECT payload FROM analyses WHERE key = ?", (key,)).fetchone()
        return Analysis.from_json(json.loads(row["payload"])) if row else None

    def save_analysis(self, key: str, analysis: Analysis) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO analyses (key, payload, confidence, created_at) VALUES (?,?,?,?)",
            (
                key,
                json.dumps(analysis.to_dict(), ensure_ascii=False),
                analysis.refund_confidence,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._index_tags(key, analysis)
        self.conn.commit()

    # -- 태그 -----------------------------------------------------------
    def _index_tags(self, key: str, analysis: Analysis) -> None:
        """분석 1건의 태그를 tags 표에 펼칩니다.

        분석을 다시 저장하면 태그도 갈아끼워야 합니다. 지우지 않고 넣기만
        하면 예전 분석의 태그가 남아 검색 결과에 유령이 섞입니다.
        """
        self.conn.execute("DELETE FROM tags WHERE key = ?", (key,))

        row = self.conn.execute(
            "SELECT * FROM precedents WHERE key = ?", (key,)
        ).fetchone()
        tax = ""
        if row is not None:
            tax = _row_to_precedent(row).resolved_tax_type

        pairs: list[tuple[str, str]] = []
        if tax:
            pairs.append(("tax", tax))
        for value in analysis.applicable_industries:
            pairs.append(("industry", value))
        for value in analysis.issue_tags:
            pairs.append(("issue", value))
        if analysis.outcome:
            pairs.append(("outcome", analysis.outcome))
        if analysis.claim_basis and analysis.claim_basis != "해당없음":
            pairs.append(("basis", analysis.claim_basis))

        self.conn.executemany(
            "INSERT OR IGNORE INTO tags (key, kind, value) VALUES (?,?,?)",
            [(key, kind, v) for kind, value in pairs
             for v in [str(value).strip()] if v],
        )

    def rebuild_tags(self) -> int:
        """tags 표를 analyses에서 통째로 다시 만듭니다.

        세목을 뒤늦게 채웠거나 태그 규칙을 바꿨을 때 씁니다.
        """
        self.conn.execute("DELETE FROM tags")
        rows = self.conn.execute("SELECT key, payload FROM analyses").fetchall()
        for row in rows:
            self._index_tags(row["key"], Analysis.from_json(json.loads(row["payload"])))
        self.conn.commit()
        return len(rows)

    def tag_counts(self, kind: str, limit: int | None = None) -> list[tuple[str, int]]:
        """태그별 판례 수. 많은 것부터."""
        sql = (
            "SELECT value, COUNT(*) AS n FROM tags WHERE kind = ? "
            "GROUP BY value ORDER BY n DESC, value ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [(r["value"], int(r["n"])) for r in self.conn.execute(sql, (kind,))]

    def keys_with_tags(self, wanted: list[tuple[str, str]]) -> set[str] | None:
        """주어진 (kind, value) 태그를 **모두** 가진 판례 키.

        태그를 고를수록 좁아지는 쪽이 사람이 기대하는 동작이라 AND로 겁니다.
        고른 태그가 없으면 None을 돌려주고, 호출부는 '필터 없음'으로 봅니다.
        """
        if not wanted:
            return None
        result: set[str] | None = None
        for kind, value in wanted:
            rows = self.conn.execute(
                "SELECT key FROM tags WHERE kind = ? AND value = ?", (kind, value)
            )
            found = {r["key"] for r in rows}
            result = found if result is None else (result & found)
            if not result:
                break
        return result or set()

    def all_tags(self) -> dict[str, dict[str, list[str]]]:
        """판례별 태그 전체. 대시보드가 한 번에 읽습니다.

        판례마다 tags_for()를 부르면 화면 한 장에 수백 번 질의하게 됩니다.
        """
        out: dict[str, dict[str, list[str]]] = {}
        rows = self.conn.execute(
            "SELECT key, kind, value FROM tags ORDER BY kind, value"
        )
        for r in rows:
            out.setdefault(r["key"], {}).setdefault(r["kind"], []).append(r["value"])
        return out

    def tags_for(self, key: str) -> dict[str, list[str]]:
        rows = self.conn.execute(
            "SELECT kind, value FROM tags WHERE key = ? ORDER BY kind, value", (key,)
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["kind"], []).append(r["value"])
        return out

    # -- 알림 -----------------------------------------------------------
    def was_notified(self, key: str, channel: str, company: str = "") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE key=? AND channel=? AND company=?",
            (key, channel, company),
        ).fetchone()
        return row is not None

    def mark_notified(self, key: str, channel: str, company: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications (key, channel, company, sent_at) VALUES (?,?,?,?)",
            (key, channel, company, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # -- 메타 -----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?,?,?)",
            (key, value, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def meta_updated_at(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT updated_at FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["updated_at"] if row else None

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "precedents": one("SELECT COUNT(*) FROM precedents"),
            "analyses": one("SELECT COUNT(*) FROM analyses"),
            "notifications": one("SELECT COUNT(DISTINCT key) FROM notifications"),
        }

    # -- DART 기업 색인 --------------------------------------------------
    def save_companies(self, companies: list[Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        rows = [
            (
                c.corp_code, c.corp_name, c.corp_cls, c.stock_code, c.bizr_no,
                c.jurir_no, c.induty_code, c.adres, c.est_dt, c.ceo_nm, c.acc_mt, now,
            )
            for c in companies
        ]
        self.conn.executemany(
            """INSERT OR REPLACE INTO companies
               (corp_code, corp_name, corp_cls, stock_code, bizr_no, jurir_no,
                induty_code, adres, est_dt, ceo_nm, acc_mt, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def indexed_corp_codes(self) -> set[str]:
        return {
            r["corp_code"]
            for r in self.conn.execute("SELECT corp_code FROM companies").fetchall()
        }

    def companies_by_industry(
        self, ksic_prefixes: list[str], limit: int = 50, audited_only: bool = False
    ) -> list[sqlite3.Row]:
        """업종코드 접두사로 회사를 찾습니다.

        판례 분석은 2자리 KSIC를 내놓고 DART의 induty_code는 더 깊은 자릿수라,
        접두사 일치로 맞춥니다.
        """
        prefixes = [p for p in (s.strip() for s in ksic_prefixes) if p.isdigit()]
        if not prefixes:
            return []
        where = " OR ".join("induty_code LIKE ?" for _ in prefixes)
        params: list[Any] = [f"{p}%" for p in prefixes]
        sql = f"SELECT * FROM companies WHERE ({where})"
        if audited_only:
            sql += " AND corp_cls = 'E'"
        sql += " ORDER BY corp_cls, corp_name LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def company_stats(self) -> dict[str, int]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        by_cls = {
            r["corp_cls"] or "?": r["n"]
            for r in self.conn.execute(
                "SELECT corp_cls, COUNT(*) n FROM companies GROUP BY corp_cls"
            ).fetchall()
        }
        with_induty = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM companies WHERE induty_code <> ''"
            ).fetchone()[0]
        )
        return {"total": total, "with_industry": with_induty, **by_cls}

    def unnotified_keys(self, channel: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT key FROM notifications WHERE channel = ?", (channel,)
        ).fetchall()
        sent = {r["key"] for r in rows}
        allk = {r["key"] for r in self.conn.execute("SELECT key FROM analyses").fetchall()}
        return allk - sent

    # -- 조회 -----------------------------------------------------------
    def analyzed_pairs(self, dedupe: bool = False) -> list[tuple[Precedent, Analysis]]:
        """저장된 판례 + 분석을 되살립니다 (backfill용).

        본문(body)만 빼고 채웁니다. 알림 렌더링과 업종 매칭에는 제목·기관·
        사건번호와 분석 결과만 쓰이지만, 세목·본문출처는 화면에 표시되므로
        함께 실어야 합니다.

        dedupe=True면 같은 판결의 다른 출처본을 빼고 대표만 돌려줍니다.
        알림을 만들 때는 반드시 켜야 한 판결이 두 번 발송되지 않습니다.
        """
        rows = self.conn.execute(
            """SELECT p.key, p.source, p.doc_id, p.title, p.case_no, p.court,
                      p.decided_on, p.url, p.tax_type, p.body_source, a.payload
               FROM precedents p JOIN analyses a ON a.key = p.key
               ORDER BY p.decided_on DESC"""
        ).fetchall()

        skip = self.duplicate_secondaries() if dedupe else set()
        pairs: list[tuple[Precedent, Analysis]] = []
        for r in rows:
            if r["key"] in skip:
                continue
            prec = Precedent(
                source=r["source"],
                doc_id=r["doc_id"],
                title=r["title"] or "",
                case_no=r["case_no"] or "",
                court=r["court"] or "",
                decided_on=_parse_kdate(r["decided_on"]),
                summary="",
                body="",
                url=r["url"] or "",
                tax_type=r["tax_type"] or "",
                body_source=r["body_source"] or "",
            )
            pairs.append((prec, Analysis.from_json(json.loads(r["payload"]))))
        return pairs

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.key, p.title, p.case_no, p.court, p.decided_on, p.url,
                      a.confidence
               FROM precedents p LEFT JOIN analyses a ON a.key = p.key
               ORDER BY p.fetched_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
