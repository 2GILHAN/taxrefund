# 법인세 판례 → 경정청구 알림 봇

최근 **법인세 판례·법령해석례**를 국가법령정보센터 OPEN API로 수집해서,
Claude가 *"같은 업종의 다른 회사도 경정청구로 세금을 돌려받을 여지가 있는 건인지"* 를
판단하고, 워치리스트에 등록한 회사와 매칭되면 알림을 보냅니다.

```
law.go.kr OPEN API  →  Claude 분석  →  업종 매칭  →  디스코드 알림
   판례·해석례          경정청구 실익      워치리스트     + 청구기한 계산
```

> ⚠️ 이 봇의 출력은 **참고자료**입니다. 실제 경정청구 전에는 반드시 세무대리인의 검토를 받으세요.

---

## 1. 설치

```powershell
cd c:\_project\taxrefund
python -m pip install -r requirements.txt
copy .env.example .env
```

## 2. 키 두 개 발급

### (1) 국가법령정보 OPEN API — 무료

1. <https://open.law.go.kr/LSO/openApi/cuAskList.do> 에서 활용 신청
2. `.env`의 `LAW_API_OC`에 가입한 이메일의 `@` 앞부분을 입력
   (예: `2gilhan@naver.com` → `LAW_API_OC=2gilhan`)

> ⚠️ **`Referer` 헤더가 없으면 무조건 거부됩니다.**
> OC가 맞아도 다음 오류가 나옵니다:
>
> ```
> 사용자 정보 검증에 실패하였습니다.
> OPEN API 호출 시 사용자 검증을 위하여 정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요.
> ```
>
> 메시지가 IP 등록 문제처럼 보이지만 **실제 원인은 헤더 검사**입니다. 존재하지 않는
> OC로 호출해도 똑같은 메시지가 나오기 때문에 원인을 특정하기 어렵습니다.
> `taxrefund/lawgo.py`가 `Referer: https://www.law.go.kr/`를 항상 보내도록 해두었고,
> 이 헤더가 사라지지 않도록 테스트로 고정해 두었습니다
> (`tests/test_pipeline.py::TestLawGoHeaders`).

### (2) 분석 모델 키 — Gemini(무료) 또는 Claude(유료)

기본값은 **Gemini**입니다. 무료 등급이 있어 카드 등록 없이 바로 시작할 수 있습니다.

<https://aistudio.google.com/apikey> 에서 발급(`AIza...`) 후 `.env`의 `GEMINI_API_KEY`에 입력.

```yaml
# config.yaml
analyze:
  provider: gemini            # gemini | claude
  gemini_model: gemini-2.5-flash
  claude_model: claude-opus-5
  request_interval_sec: 6.0   # 무료 등급 RPM 한도 회피용 호출 간격
```

쓸 수 있는 모델은 키를 넣은 뒤 확인할 수 있습니다:

```powershell
python -m taxrefund models
```

**무료 등급 한도 대응** — 분당(RPM)·일일(RPD) 제한이 있어 이렇게 처리합니다.

| 상황 | 동작 |
|---|---|
| 호출 간격 | `request_interval_sec`(기본 6초)만큼 띄워서 RPM 한도를 피합니다 |
| 분당 한도 초과 | 응답이 알려준 `retryDelay`만큼 기다렸다 재시도(기본 3회) |
| **일일 한도 소진** | 그때까지 분석한 건은 살려서 알림을 보내고 **이번 사이클만 중단**. 다음 날 이어서 진행 |
| 인증 실패 / 없는 모델 | 원인을 구분해 안내하고 설정 대기 상태로 |

분당/일일 구분은 메시지 문자열이 아니라 429 응답의 구조화된 `QuotaFailure`
(`quotaId`에 `PerDay`/`PerMinute`가 명시됨)로 판정합니다. 로그에 실제 한도가 찍힙니다.

```
Gemini 무료 등급 일일 한도를 소진했습니다(하루 20건). 모델 'gemini-3.7-flash'.
내일 이어서 자동으로 분석합니다. 지금까지 분석한 6건으로 진행합니다.
```

> **`gemini-3.7-flash` 무료 등급은 하루 20건**입니다(2026-08 기준, 모델마다 다름).
> 새로 등재되는 대법원 법인세 판례는 월 몇 건 수준이라 일상 운영에는 충분하지만,
> 처음 밀린 물량을 훑을 때는 며칠에 걸쳐 나눠 처리됩니다. 이미 분석한 건은
> 다시 분석하지 않으므로(중복 방지) 그냥 두면 알아서 따라잡습니다.
>
> 한도가 아쉬우면 `gemini-3.5-flash-lite` 같은 lite 계열이 보통 한도가 더 넉넉합니다
> (`python -m taxrefund models`로 목록 확인). 대신 판단 품질은 떨어질 수 있습니다.
>
> 참고: Google 무료 등급 한도는 **태평양시 자정**에 초기화됩니다(한국시간 16~17시경).
> 기본 실행 시각인 매일 08:00(KST)은 매번 다른 태평양 날짜에 걸리므로,
> 하루치 한도를 온전히 쓸 수 있습니다.

429가 자주 뜨면 `request_interval_sec`를 늘리세요. 유료 등급으로 올리면 1.0 정도로 줄여도 됩니다.

**Claude로 바꾸려면** `provider: claude`로 두고 `ANTHROPIC_API_KEY`를 채우면 됩니다
(<https://console.anthropic.com/>). 판단 품질은 더 좋지만 무료 등급이 없습니다.
프롬프트와 출력 스키마는 두 제공자가 공유하므로 결과 형식은 동일합니다.

### (3) 디스코드 봇 — 나중에 해도 됩니다

**지금 안 해도 봇은 정상 동작합니다.** 디스코드가 설정되기 전까지 알림은 콘솔·파일로만
나가고, 디스코드 채널은 *오류가 아니라 '건너뜀'* 으로 처리되어 **발송 기록을 남기지 않습니다.**
그래서 나중에 봇을 붙이면 그동안 쌓인 알림을 **하나도 놓치지 않고** 받을 수 있습니다.
(→ 아래 [10. 디스코드 봇을 나중에 붙이기](#10-디스코드-봇을-나중에-붙이기))

메시지는 **봇 계정 이름으로** 나갑니다. 게이트웨이(WebSocket) 접속 없이 REST API만
쓰므로 `discord.py` 같은 라이브러리가 필요 없습니다.

1. <https://discord.com/developers/applications> → **New Application**
2. 좌측 **Bot** 탭 → **Reset Token** → 토큰 복사 → `.env`의 `DISCORD_BOT_TOKEN`
3. **General Information**의 **Application ID**를 아래 `<APP_ID>`에 넣어 열고 서버에 초대
   (권한 `18432` = Send Messages + Embed Links):

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=18432
   ```

4. 디스코드 앱에서 **사용자 설정 → 고급 → 개발자 모드** ON →
   알림 받을 채널 우클릭 → **채널 ID 복사** → `.env`의 `DISCORD_CHANNEL_ID`

`python -m taxrefund check`가 4단계 안내를 그대로 출력하니 그때 보고 따라 하셔도 됩니다.

> 봇 대신 웹훅(`DISCORD_WEBHOOK_URL`)도 지원하지만 보조 수단입니다.
> 봇 토큰이 있으면 항상 봇 모드가 우선합니다.

### 설정 점검

```powershell
python -m taxrefund check
```

디스코드가 설정되어 있으면 토큰을 검증하고 봇이 그 채널에 접근할 수 있는지까지 확인해
`봇이름 → #채널명`을 출력합니다(메시지는 보내지 않음). 아직 미설정이면 `[대기]`로
표시되고 설정 절차를 안내하며, **문제 개수에 포함되지 않습니다.**

```powershell
python -m taxrefund test-notify --channels discord   # 실제로 한 건 보내 보기
```

## 3. 실행

### 상시 실행 (서버 모드) — 권장

```powershell
python -m taxrefund serve
```

- `config.yaml`의 `serve.daily_at`(기본 매일 08:00)에 맞춰 반복 실행합니다.
- 시작 직후 한 번 바로 돌고, 이후 일정에 맞춰 대기합니다.
- 시작·종료 시 디스코드로 상태 메시지를 보냅니다(`notify.startup_ping`).
- 로그는 `logs/taxrefund.log`에 회전 저장(2MB × 5개)됩니다.
- **Ctrl+C**를 누르면 진행 중인 작업을 마치고 가동시간·누적 알림을 정리해 종료합니다.
- 일시적 오류는 삼키고 계속 돌되, **연속 3회 실패하면 디스코드로 경고**를 보냅니다.

**설정이 덜 됐어도 그냥 띄워 두면 됩니다.** `ANTHROPIC_API_KEY`가 없거나
`LAW_API_OC` 등록이 안 된 상태면 서버는 멈추지 않고 **설정 대기** 상태로 들어갑니다.

- 대기 중에는 정규 일정(예: 매일 08:00) 대신 **60초마다 `.env`를 다시 확인**합니다.
- 대기 사유는 디스코드로 한 번만 알립니다(사유가 바뀌면 다시 알림).
- 값을 채우면 **재시작 없이** 1분 안에 스스로 재개하고 `▶️ 재개`를 보냅니다.

```
⏸️ 법령정보 API 설정 대기 중
LAW_API_OC가 비어 있습니다. ...
`.env`의 `LAW_API_OC`를 채우고, open.law.go.kr에 이 서버의 공인 IP를
등록하면 자동으로 재개합니다. 서버를 다시 시작할 필요는 없습니다.
```

```powershell
python -m taxrefund serve --interval 360        # 6시간마다
python -m taxrefund serve --daily-at 09:30      # 매일 09:30
python -m taxrefund serve --no-run-on-start     # 시작 직후 실행 생략
python -m taxrefund serve --sample              # 샘플 데이터로 동작 확인
```

백그라운드로 띄우고 상태 보기:

```powershell
# 시작 (PID를 기억해 두세요)
Start-Process -WindowStyle Hidden python -ArgumentList "-m","taxrefund","serve" `
  -WorkingDirectory c:\_project\taxrefund -PassThru | Select-Object Id

# 로그 실시간 확인 (-Encoding UTF8 없으면 한글이 깨집니다)
Get-Content c:\_project\taxrefund\logs\taxrefund.log -Wait -Tail 20 -Encoding UTF8

# 중지
Stop-Process -Id <PID>
```

### 한 번만 실행

```powershell
python -m taxrefund run --sample     # 샘플 판례로 전체 흐름 확인
python -m taxrefund run --dry-run    # 분석까지만, 발송 안 함
python -m taxrefund run              # 정식 실행
```

| 명령 | 설명 |
|---|---|
| `serve` | 일정에 맞춰 상시 실행 (+ 웹 대시보드) |
| `web` | 웹 대시보드만 실행 |
| `run` | 수집 → 분석 → 매칭 → 알림 한 사이클 |
| `crawl` | 판례를 받아 **저장만** (분석 없음). 과거 구간 채우기 |
| `status` | 수집·분석 진행 상황과 남은 기간 |
| `backfill` | 아직 못 보낸 알림을 몰아서 발송 (채널을 나중에 붙였을 때) |
| `check` | API 키·워치리스트·알림 채널·공인 IP 점검 |
| `diagnose` | 법령 OPEN API가 거부될 때 원인 좁히기 |
| `models` | 이 키로 쓸 수 있는 Gemini 모델 목록 |
| `headlines` | 한 줄 요지가 없는 기존 분석에 제목을 채움 |
| `list` | 최근 수집·분석한 판례 목록 |
| `test-notify` | 가짜 알림을 보내 채널 연결 확인 |

`run` 옵션: `--sample` `--limit N` `--dry-run` `--force` `--channels discord` `--every 분`
`crawl` 옵션: `--since-days N` `--max-pages N`
`serve` 옵션: `--sample` `--limit N` `--force` `--channels` `--daily-at HH:MM` `--interval 분` `--no-run-on-start`

## 4. 대상 회사 찾기 — DART 기업 색인

판례가 어떤 업종에 걸리는지는 분석 결과(`ksic_codes`)로 나옵니다.
그 업종의 **외부감사대상 법인**을 찾으려면 DART 색인이 필요합니다.

DART에는 **업종으로 검색하는 API가 없습니다.** 기업별 업종코드는
`company.json`에 있는데 고유번호를 하나씩 넣어야 나옵니다. 그래서 한 번
긁어 로컬 SQLite에 색인해 두고 조회합니다.

```powershell
# 1) https://opendart.fss.or.kr/ 에서 무료 발급 -> .env의 DART_API_KEY
# 2) 색인 구축 (일일 한도에 걸리면 그때까지 저장하고 멈춤)
python -m taxrefund companies build

# 3) 다음 날 같은 명령으로 이어서 (이미 받은 곳은 건너뜀)
python -m taxrefund companies build

python -m taxrefund companies stats                    # 색인 현황
python -m taxrefund companies find 26 27               # 업종코드로 조회
python -m taxrefund companies find 41 --audited-only   # 비상장 외감법인만
```

**호출 수를 줄이는 방법** — `corpCode.xml`의 전체 목록(10만여 곳)에는 폐업·휴면
법인이 많이 섞여 있습니다. `list.json`으로 **최근 2년 사업보고서를 낸 기업**만
먼저 추리면 외감/상장 대상만 남아 대상이 크게 줄어듭니다. 이 목록은 저장해 두고
재사용합니다(`--refresh-filers`로 갱신).

DART 일일 한도는 20,000건입니다. 한도에 걸리면 수집분을 저장하고 멈추므로,
며칠에 걸쳐 나눠 채우면 됩니다.

| 저장 항목 | 쓰임 |
|---|---|
| `induty_code` | 판례 업종과 접두사 매칭 (분석은 2자리, DART는 5자리) |
| `corp_cls` | `E`=비상장 외감, `Y/K/N`=상장 |
| **`acc_mt`(결산월)** | 회사별 정확한 경정청구 기한 계산 |
| `bizr_no`, `adres`, `ceo_nm` | 실무 확인용 |

> 공개 DB로 뽑은 연락처에 **영리목적 광고성 정보를 보내는 것은 정보통신망법
> 제50조 규제 대상**입니다. 리스트업까지만 활용하고, 접촉은 기존 관계나 별도
> 동의 경로로 진행하세요.

## 5. 설정 (`config.yaml`)

관심 대상을 워치리스트에 등록합니다. 판례의 적용 업종이 `전업종`이면 전체가 매칭되고,
그 외에는 **업종명**·**키워드**·**표준산업분류 코드**가 겹칠 때 매칭됩니다.
여기에 하나도 걸리지 않으면 점수가 아무리 높아도 알림이 나가지 않습니다.

기본값은 실제 고객사 대신 **업종군 10개**를 깔아 둔 상태입니다.

```yaml
watchlist:
  - name: 제조업                      # 회사가 아니라 업종 버킷
    industry: 제조업
    ksic: ["10", "11", ...]
    keywords: [제조업, 전자부품, 반도체, 자동차부품, ...]
```

실제 고객사가 정해지면 회사 단위로 바꾸십시오.

```yaml
watchlist:
  - name: 한국전자부품(주)
    industry: 전자부품 제조업
    ksic: ["26", "27"]          # 표준산업분류 2자리 접두사
    keywords: [반도체, 연구인력개발비, 감가상각]
```

> `ksic`는 분석의 `ksic_codes`와 대조합니다. 손으로 넣은 분석에는 그 값이 비어 있어
> 지금은 **업종명·키워드 매칭만** 작동합니다. DART 색인(§4)을 붙이면 업종코드로
> 실제 대상 법인을 뽑을 수 있습니다.

알림 문턱값과 일정:

```yaml
notify:
  min_confidence: 60                   # 경정청구 실익 점수 (0-100)
  require_generalizable: true          # 사실관계가 특수한 판례는 제외
  channels: [console, file, discord]   # console | file | discord | slack | email
  startup_ping: true                   # 서버 기동/종료를 디스코드로 알림

serve:
  daily_at: "08:00"      # 매일 이 시각 (비우면 interval_minutes 사용)
  interval_minutes: 360
  run_on_start: true
  log_file: logs/taxrefund.log
```

## 6. 알림에 담기는 것

디스코드에는 판례 1건당 임베드 하나로 들어가고, 점수에 따라 색이 달라집니다
(80점 이상 🔴 · 70–79 🟠 · 그 외 🔵). 한 메시지에 4건씩 나눠 보내며 레이트리밋은
자동으로 재시도합니다.

- **한 줄 요지 제목** — 법원이 붙인 사건명은 `법인세부과처분취소`류가 대부분이라
  목록에서 서로 구분되지 않습니다. 그래서 모델이 쟁점과 결론이 드러나는 한 줄을
  따로 뽑아 제목으로 씁니다. 사건명은 부제로 남아 식별에 씁니다.

  | | |
  |---|---|
  | 사건명(법원) | 법인세부과처분취소 |
  | **표시 제목** | **직원매장 할인액은 접대비 아닌 매출에누리** |

  스키마 추가 전에 분석한 건은 한 줄 요지가 없어 사건명이 그대로 보입니다.
  `python -m taxrefund headlines` 로 채울 수 있습니다(저장된 요약만 넣어 가벼운 요청).

- 쟁점 요약과 결론(납세자승/과세관청승/일부승)
- **경정청구 실익 판단**과 그 근거, 0–100 점수
- 적용 가능 업종 + 매칭된 자사 회사와 매칭 이유
- **담당자가 확인할 체크리스트** (예: "2021~2025 사업연도 연구개발비 중 기존 제품 개량으로 분류해 제외한 금액이 있는지")
- **청구 기한** — 코드에서 결정적으로 계산합니다(모델에 맡기지 않음)
  - 통상적 경정청구(국기법 §45조의2①): 법정신고기한 후 5년.
    12월 결산 법인 기준으로 아직 열려 있는 사업연도와 가장 먼저 만료되는 날짜를 계산하고,
    90일 이내로 임박하면 경고를 붙입니다.
  - 후발적 사유(§45조의2②): 사유를 안 날부터 3개월. 선고일 기준 참고값을 제시하되,
    기산점이 판결 확정일 등이라는 점을 함께 안내합니다.

## 7. 필터링 기준

다음을 **모두** 만족해야 알림이 나갑니다.

1. 법인세 과세표준·세액 계산에 영향을 주는 쟁점일 것
2. 납세자에게 유리한 방향이고, 기존 과세 실무를 뒤집거나 좁히는 판단일 것
3. 개별 사실관계가 아니라 법령 해석·법리에 관한 판단일 것 (`require_generalizable`)
4. 실익 점수가 `min_confidence` 이상일 것
5. 워치리스트 회사 중 최소 1곳과 매칭될 것

과세관청 승소 판례, 기존 실무를 그대로 확인한 판례, 부가세·소득세만 다룬 판례는 걸러집니다.

## 8. 폰에서 보기 (웹 대시보드)

`serve`를 돌리면 대시보드가 함께 뜹니다. 별도 실행도 가능합니다.

```powershell
python -m taxrefund web
```

| 화면 | 내용 |
|---|---|
| `/` **알림** | 기준을 통과한 건만. 통계 타일 + 세목·업종 바로가기 + 점수 카드 |
| `/all` **전체** | 분석한 모든 건 + **탈락 사유**. 검색·태그로 좁혀 보기 |
| `/browse` **분류** | 세목·업종·쟁점·결과를 축별로 훑어보는 색인 |
| `/status` **상태** | 수집·발송 현황, 일정, 워치리스트, 공인 IP |
| `/d/<key>` 상세 | 실익 근거, 확인 체크리스트, 청구 기한, 원문 링크 |
| `/api/alerts.json` | JSON (다른 도구와 연동용) |
| `/health` | 모니터링용 (토큰 불필요) |

카드에 붙는 태그는 **세목(파랑) · 업종 · 주제** 순서이고 **누르면 그 조건으로 좁힌
목록**이 열립니다. 카드 아래 `해당 업종군`은 워치리스트에서 걸린 항목입니다.

**홈의 통계 타일은 누르면 해당 목록으로 갑니다.**

| 타일 | 이동 |
|---|---|
| 수집 / 분석 | `/all?f=all` — 전체 |
| 알림 대상 | `/all?f=alert` — 기준 통과분만 |
| 미발송 | `/all?f=pending` — 아직 안 보낸 것만 |

### 알림 세목 (`notify.tax_types`)

기본값은 **법인세 · 부가가치세 · 상속증여세**입니다. 여기 없는 세목(교육세·종합소득세
등)은 대시보드와 분류 화면에는 남되 알림에서만 빠지고, 탈락 사유에
`알림 세목 아님 (교육세)`처럼 표시됩니다.

```yaml
notify:
  tax_types: [법인세, 부가가치세, 상속증여세]   # []로 두면 세목으로 거르지 않음
```

세목은 국세법령정보시스템이 준 값이 1순위이고, 없으면 사건명에서 읽습니다.
국세청이 같은 세목을 여러 표기로 내보내는 경우(`교육세`/`교육세법`)는 하나로
합칩니다(`models.TAX_TYPE_ALIASES`).

> 예전에는 분석의 `is_corporate_tax`로 걸렀습니다. 그러면 세목을 넓혀도 분석을
> 다시 하지 않는 한 통과할 수가 없어서, 판례에 붙은 세목으로 거르도록 바꿨습니다.

### 태그와 검색 (`/all`)

목록 위에 **검색창**과 **태그 줄 네 개**가 있습니다.

| 태그 | 내용 | 나오는 곳 |
|---|---|---|
| **세목** | 법인세 · 부가가치세 · 상속증여세 · 교육세 ... | 국세청 세목 (없으면 사건명에서 추정) |
| **업종** | 제조업 · 건설업 · 지주회사 · 전업종 ... | 분석의 `applicable_industries` |
| **쟁점** | 부당행위계산 부인 · 손금불산입 · 국내원천소득 ... | 분석의 `issue_tags` |
| **결과** | 납세자승 · 과세관청승 · 일부승 | 분석의 `outcome` |

* 태그를 누르면 켜지고, 다시 누르면 꺼집니다.
* **같은 줄에서 여러 개**를 고르면 넓어집니다 (법인세 **또는** 부가가치세).
* **다른 줄끼리**는 좁아집니다 (법인세 **이면서** 제조업).
* 검색어는 띄어쓰기로 나누어 **모두 포함**된 건만 남깁니다. 대소문자는 무시합니다.
  제목·사건명·사건번호·법원·쟁점 요약·실익 근거·태그가 검색 대상입니다.
* 검색과 태그, 상태 필터(알림/미발송/제외)는 **같이 걸립니다.** 조건이 하나라도
  걸려 있으면 목록 위에 `필터 해제` 링크가 뜹니다.
* 주소가 곧 필터라 **북마크로 저장**할 수 있습니다.
  예: `/all?tax=법인세&industry=제조업&q=손금`

상세 화면 아래쪽 **태그** 박스의 태그를 누르면 같은 태그가 붙은 목록으로 갑니다.

> 태그는 분석을 저장할 때 색인됩니다. 세목 규칙을 바꿨거나 색인이 어긋났다면
> `Store.rebuild_tags()`로 다시 만들 수 있습니다.

### 같은 판결이 두 번 뜨는 경우

법령정보센터와 국세법령정보시스템이 **같은 판결을 각자 다른 일련번호로** 내놓습니다.
그대로 두면 한 판결이 목록에 두 번 뜨고 알림도 두 번 갑니다.

봇은 **사건번호와 선고일이 모두 같으면 같은 판결로 보고**, 본문이 긴 쪽을 대표로 남깁니다.

* 목록에는 둘 다 남되 대표가 아닌 쪽에 **`중복(같은 판결)`** 배지가 붙습니다.
* **알림은 대표 한 건만** 나갑니다 (`Store.analyzed_pairs(dedupe=True)`).
* 사건번호 표기가 우연히 겹치는 다른 법원 사건은 선고일이 달라 걸러집니다.

```python
store.duplicate_groups()      # {'2022두38199': ['prec:621975', 'prec:619193'], ...}
store.duplicate_secondaries() # 대표가 아닌 키
```

### 복사 버튼

상세 화면(`/d/<key>`) **첫 박스 안에 `복사` 버튼 하나**만 있습니다.
누르면 아래가 한 번에 복사됩니다.

```
[85점] 법인세부과처분취소

구분: 판례 · 대법원
사건번호: 2025두34772
선고일: 2026-02-12
결론: 납세자승
청구 근거: 통상적 경정청구
영향도: 중
적용 업종: 전업종

■ 쟁점 요약
...

■ 경정청구 실익
...

원문: https://www.law.go.kr/DRF/lawService.do?...
```

확인 체크리스트와 청구 기한은 화면에서 보는 용도라 복사 분량에 넣지 않았습니다.
누르면 버튼이 `✅ 복사됨`으로 잠깐 바뀝니다.

### 복사가 한 번에 되게 하려면 — HTTPS 켜기

브라우저는 **HTTPS(보안 컨텍스트)에서만** 클립보드 API를 허용합니다.
이 대시보드는 기본이 HTTP라, 특히 **iPhone에서는 자동 복사가 불가능**합니다.

| 환경 | 동작 |
|---|---|
| HTTPS | `navigator.clipboard` → 버튼 한 번에 복사 |
| HTTP + 데스크톱 | `execCommand` 폴백으로 대개 동작 |
| **HTTP + iOS** | **불가능.** 버튼을 누르면 내용이 아래에 펼쳐지고 전체 선택되므로, 길게 눌러 `복사` |

> iOS Safari는 `execCommand('copy')`가 **`true`를 반환하면서도 실제로는 복사하지 않습니다.**
> 반환값을 믿으면 "복사됨"이라고 거짓 안내를 하게 되므로, iOS에서는 아예 시도하지 않고
> 곧바로 수동 복사 경로로 보냅니다.

**Tailscale HTTPS를 켜면 해결됩니다** (무료, 1회 설정):

1. <https://login.tailscale.com/admin/dns> → **HTTPS Certificates** → **Enable**
2. `config.yaml`에서 `web.host: local` 로 변경 (네트워크에 직접 노출하지 않음)
3. PC에서 한 줄 실행:

   ```powershell
   tailscale serve --bg --https=443 http://127.0.0.1:8787
   ```

4. 접속 주소가 `https://<기기이름>.<tailnet>.ts.net/` 으로 바뀝니다

설정하면 봇이 이를 자동으로 감지해 시작 로그·디스코드 알림에 HTTPS 주소를 안내하고,
대시보드 **상태** 화면도 "HTTPS로 서비스 중"으로 바뀝니다.

이 구성이 보안상 가장 낫습니다:

| | HTTP 직접 노출 | **HTTPS + Serve** |
|---|---|---|
| 리스닝 | Tailscale IP (`100.x.x.x:8787`) | `127.0.0.1:8787` — **어떤 네트워크에도 안 뜸** |
| 전송 | 평문 (토큰 노출) | TLS 암호화 (Let's Encrypt) |
| 복사 버튼 | iOS에서 불가 | 한 번에 동작 |

해제: `tailscale serve --https=443 off`

`/all`에서는 **전체 / 알림 대상 / 미발송 / 제외됨** 칩으로 바로 전환됩니다.
제외된 건에는 사유가 붙어 있어(`점수 미달 (41 < 60)`, `일반화 어려움` …)
`min_confidence` 문턱값을 조정할 때 근거가 됩니다.

모바일 우선으로 만들었습니다 — 반응형, 다크모드 자동, 홈 화면에 추가하면 앱처럼 씁니다.

### 접속 주소 — `web.host`

```yaml
web:
  host: tailscale   # tailscale | local | 0.0.0.0 | 특정 IP
  port: 8787
```

- **`tailscale`** (권장) — Tailscale 주소에만 바인드합니다. 폰에 Tailscale을 깔면
  **집 밖에서도** 접속되고, 인터넷에는 전혀 열리지 않습니다.
- `local` — 이 PC에서만.
- `0.0.0.0` — 모든 인터페이스. **공인 IP를 직접 쓰는 회선이면 인터넷에 그대로 열립니다.**
  그런 환경이면 시작 시 경고를 출력합니다.

> ⚠️ 이 대시보드는 **HTTP(암호화 없음)** 입니다. `WEB_TOKEN`을 설정하면 토큰 없이는
> 접속되지 않지만, 토큰 자체가 평문으로 오갑니다. 공개망에 직접 노출하지 마세요.
> Tailscale 경로는 자체 암호화되므로 이 문제가 없습니다.

`.env`에 `WEB_TOKEN`을 넣으면 `?t=<토큰>`으로만 접속됩니다(최초 1회 접속 후 쿠키 저장).

## 9. 본문은 두 곳에서 옵니다

법령 API의 판례는 **출처(`데이터출처명`)에 따라 본문 제공 여부가 갈립니다.**

| 출처 | 법령 API 상세 조회 | 본문을 실제로 가진 곳 |
|---|---|---|
| **대법원** | 성공 | 법령정보센터 (판시사항·판결요지·전문) |
| **국세법령정보시스템** | **실패** | 국세청 `taxlaw.nts.go.kr` |

"법인세" 검색 기준 대략 **대법원 22% / 국세법령정보시스템 78%** 입니다.
법령정보센터는 국세청에서 **목록만 연계받아 색인**하고 본문은 갖고 있지 않습니다.
`판례상세링크`가 가리키는 페이지도 국세청 화면을 iframe으로 감싼 껍데기입니다.

그래서 봇은 **국세청에서 본문을 직접 보충합니다.**

```
수집 (법령 API)  →  본문 없는 건을 사건번호로 국세청 검색  →  본문·세목·판시요지 저장
```

```powershell
# 밀린 것 전부 보충 (건당 1초, 400건이면 약 15분)
python -m taxrefund bodies --all

# 진행 상황
python -m taxrefund status
```

`run`/`serve`로 도는 매일 사이클에도 이 단계가 들어 있어(`nts_per_run_limit`,
기본 60건) 새로 들어온 건은 자동으로 채워집니다.

### 알아 둘 점

* 국세청 쪽은 **공식 OPEN API가 아닙니다.** 화면이 쓰는 내부 요청(`POST /action.do`)을
  그대로 호출합니다. 국세청이 화면을 고치면 조용히 멈출 수 있고, 그때는 수집·분석·알림은
  그대로 돌고 본문 보충만 건너뜁니다. `crawl.nts_enabled: false`로 끌 수 있습니다.
* 호출 간격은 기본 1초입니다(`crawl.nts_interval_sec`). 사람이 브라우저로 보는 속도를
  넘기지 않으려는 것이니 줄이지 마세요.
* 국세청 검색은 형태소 단위라 사건번호를 넣어도 **엉뚱한 건이 섞여 나옵니다.**
  받아온 사건번호를 정규화해 대조가 맞는 건만 씁니다. 안 맞으면 버립니다.
  (`대법원-2026-두-30683`, `대법원2026두30683`, `2026두30683`은 같은 사건으로 봅니다.)
* 세 번 찾아도 안 나오는 건은 더 두드리지 않습니다(`body_tries`).

### 덤으로 얻는 것

국세청 응답에는 법령 API에 없는 값이 둘 붙어 옵니다.

* **세목** (`법인세`/`부가가치세`/`상속증여세`...) — 대시보드 세목 태그의 근거입니다.
  국세청이 `기타`로 준 건은 사건명에서 다시 읽습니다(교육세 등).
* **판시요지** — 국세청이 정리한 한 줄 요지. 본문이 짧은 심리불속행 건에서 특히 유용합니다.

상세 화면의 **본문 출처**란에 그 건의 본문이 어디서 왔는지 표시됩니다.

## 10. 공인 IP가 바뀌면

`Referer` 문제와 별개로, 법제처가 IP 검증을 적용하는 신청 유형도 있습니다.
공인 IP가 유동이면 그때 문제가 될 수 있어 봇이 변동을 감지합니다.

- 법령 API 호출이 **성공했을 때의 공인 IP를 기억**해 둡니다.
- 이후 IP가 달라지면 **디스코드로 먼저 알립니다** — 새 IP를 그대로 복사해 등록하면 됩니다.

  ```
  🌐 공인 IP가 바뀌었습니다
  이 서버의 공인 IP가 182.221.152.12 → 121.130.4.77 로 바뀌었습니다.
  국가법령정보 OPEN API는 등록된 IP로만 호출을 허용하므로,
  open.law.go.kr 에서 121.130.4.77 를 등록해야 수집이 재개됩니다.
  ```

- IP 검증 실패로 대기 상태에 들어가면, 대기 알림에도 **현재 IP**가 함께 들어갑니다.
- 대시보드 `/` 상단과 `/status`에도 변동 배너가 뜹니다.
- `python -m taxrefund check`가 현재 공인 IP와 등록된 IP를 비교해 보여줍니다.

### 거부될 때 원인 좁히기

```powershell
python -m taxrefund diagnose
```

이 API는 **OC 불일치와 IP 미등록에 똑같은 오류**를 돌려주므로 응답만으로는 구분되지
않습니다(존재하지 않는 OC로 호출해도 동일한 메시지가 옵니다). `diagnose`는 대상별로
호출해 보고, 확인해야 할 항목을 순서대로 제시합니다.

- 일부 대상만 거부 → 신청 시 그 대상(판례/법령해석례)을 포함하지 않은 것
- 전부 거부 → 승인 상태 / 등록 IP / OC 값 / 신청 대상을 순서대로 확인

**근본 해결책** (봇이 대신 해줄 수 없는 부분):

| 방법 | 설명 |
|---|---|
| 고정 IP 신청 | ISP에 고정 IP를 신청하는 것이 가장 확실합니다(유료). |
| 여러 IP 등록 | 자주 바뀌는 IP 대역이 좁으면 여러 개를 미리 등록해 둡니다. |
| 클라우드 VM | 고정 IP가 붙는 VM(오라클 무료 티어 등)에서 봇을 돌립니다. |
| 그냥 두기 | 바뀔 때마다 디스코드 알림을 받고 재등록합니다. 봇은 자동 재개하므로 재등록만 하면 됩니다. |

> Tailscale은 이 문제를 해결하지 못합니다. 대시보드 접속 경로만 바꿀 뿐,
> law.go.kr로 나가는 트래픽은 여전히 회선의 공인 IP를 씁니다.
> (Tailscale exit node를 고정 IP 서버로 두면 우회는 가능합니다.)

## 11. 디스코드 봇을 나중에 붙이기

봇을 아직 안 만들었어도 그냥 돌리면 됩니다. 설계가 그걸 전제로 되어 있습니다.

**지금 (봇 없음)**

```powershell
python -m taxrefund serve
```

알림은 콘솔·파일로 나갑니다. 디스코드는 `건너뜀(미설정)`으로 처리되고
**발송 기록을 남기지 않습니다** — 이게 핵심입니다. 미설정을 '실패'로 처리하면
발송된 것으로 기록되어 나중에 받을 수 없게 됩니다.

**나중에 (봇 준비 후)** — `.env`에 두 값을 채우면 끝입니다.

```powershell
python -m taxrefund check              # 봇이름 → #채널명 확인
python -m taxrefund backfill           # 그동안 쌓인 알림 한 번에 수신
```

**서버를 켜 둔 채로 채웠다면 `backfill`도 필요 없습니다.** 서버는 매 사이클마다
`.env`를 다시 읽어서, 디스코드가 새로 켜진 걸 감지하면 연결 알림을 보내고
밀린 알림을 자동으로 발송합니다. 재시작 불필요.

```
🟢 디스코드 연결됨
이제부터 이 채널로 알림을 보냅니다. 그동안 쌓인 알림을 이어서 발송합니다.
```

`backfill`은 저장된 분석 결과에서 알림을 다시 조립하므로 Claude를 재호출하지
않습니다(추가 비용 없음). 업종 매칭과 청구 기한은 **실행 시점 기준으로 다시 계산**되어,
그사이 워치리스트를 바꿨거나 기한이 임박했으면 그게 반영됩니다.

```powershell
python -m taxrefund backfill --dry-run              # 뭐가 밀렸는지만 확인
python -m taxrefund backfill --channels discord     # 디스코드만
python -m taxrefund backfill --limit 10             # 한 번에 10건까지
```

`check`는 채널별로 몇 건이 밀려 있는지도 알려줍니다.

## 12. 과거 구간까지 모으기

수집과 분석은 **분리되어 있습니다.** 이유는 병목이 서로 다르기 때문입니다.

| 단계 | 병목 | 하루 처리량 |
|---|---|---|
| 수집 | 법령 API 호출 | 사실상 제한 없음 |
| 분석 | 모델 일일 한도 | Gemini 무료 등급 **20건** |

둘을 한 루프에서 돌리면 모델 한도가 떨어질 때마다 크롤링도 함께 멈춰서
과거 구간이 영영 채워지지 않습니다. 그래서 이렇게 나눴습니다.

```
crawl : 기간 안의 판례를 본문까지 전부 받아 DB에 쌓음
   ↓
분석 큐 (미분석 + 본문 있음, 최신순)
   ↓
run   : 큐에서 per_run_limit 만큼만 꺼내 분석 → 알림
```

수집 범위는 `config.yaml`의 `crawl.since_days`로 정합니다 (`1825` = 5년).
검색 결과가 선고일 내림차순이라, **기간 밖 건만 담긴 페이지를 만나면 거기서 멈춥니다.**
이미 본문을 받아 둔 건은 상세 조회를 건너뛰므로 매일 돌려도 새 등재분만 추가됩니다.

```powershell
python -m taxrefund crawl                  # config 기준(5년)으로 수집
python -m taxrefund crawl --since-days 3650  # 10년으로 넓히기
python -m taxrefund status                 # 남은 물량과 예상 소요일
```

`status` 출력 예:

```
  수집한 판례      :   512건
    - 본문 있음    :   180건  (분석 가능)
  분석 완료        :    20건
  분석 대기        :   160건

  하루 18건씩이면 약 9일 걸립니다.
```

`serve`를 켜 두면 매일 정해진 시각에 `crawl` → 분석(한도만큼)을 반복하므로,
밀린 물량은 며칠에 걸쳐 저절로 소화됩니다.

### 계속 실패하는 건

본문이 이상하거나 모델이 반복해서 응답을 깨뜨리는 판례가 있습니다. 그대로 두면
그 한 건이 매일 한도를 먼저 먹으므로, **3회 실패하면 분석 큐에서 뺍니다**
(`store.MAX_ANALYSIS_ATTEMPTS`). 다시 수집해도 이 기록은 지워지지 않습니다.

## 13. 중복 방지

`data/taxrefund.db`(SQLite)에 처리한 판례와 **채널별** 발송 이력을 기록합니다.
같은 판례가 같은 채널로 두 번 가지 않고, 이미 분석한 건은 재분석하지 않아 API 비용이
들지 않습니다. 다시 보내려면 `--force`를 씁니다.

## 14. 매일 자동 실행 (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

시작프로그램 폴더에 `pythonw.exe -m taxrefund serve` 바로가기를 만듭니다.
**관리자 권한이 필요 없고**, `pythonw`라 콘솔 창도 뜨지 않습니다.

- **로그온할 때마다** 봇이 자동으로 뜹니다.
- 뜬 뒤에는 `serve`가 `config.yaml`의 `serve.daily_at`(기본 **매일 08:00**)에 맞춰
  스스로 반복 실행합니다. 별도 스케줄 등록이 필요 없습니다.
- 해제: `powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove`

```powershell
# 지금 떠 있는지 확인
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*taxrefund*" } | Select-Object ProcessId

# 로그 실시간 확인
Get-Content c:\_project\taxrefund\logs\taxrefund.log -Wait -Tail 20 -Encoding UTF8
```

> 작업 스케줄러(`Register-ScheduledTask` / `schtasks`)는 이 PC에서 관리자 권한이
> 없어 등록에 실패합니다. VBS 런처도 `wscript` 실행이 막혀 동작하지 않았습니다.
> 그래서 중간 단계 없이 실행 파일을 직접 가리키는 바로가기 방식을 씁니다.

## 15. 테스트

```powershell
python -m tests.test_pipeline       # 파이프라인 전 구간·법령 API 헤더·본문 필터   22건
python -m tests.test_gemini         # Gemini 스키마·한도·오류 분기                22건
python -m tests.test_discord_serve  # 디스코드·스케줄러·백필·자동감지             42건
python -m tests.test_web_netcheck   # 대시보드 렌더링·인증·IP 감지·바인딩         31건
python -m tests.test_sdk_params     # SDK 파라미터 지원 여부                       3건
```

## 16. 구조

| 파일 | 역할 |
|---|---|
| `taxrefund/lawgo.py` | 국가법령정보 OPEN API 클라이언트 (공식 API만 사용, 스크래핑 없음) |
| `taxrefund/analyzer.py` | 공유 프롬프트·출력 스키마·예외 + provider 선택 |
| `taxrefund/analyzer_gemini.py` | Gemini 분석 (무료 등급 한도 대응) |
| `taxrefund/analyzer_claude.py` | Claude 분석 (프롬프트 캐싱, 서버사이드 폴백) |
| `taxrefund/matcher.py` | 워치리스트 매칭, 경정청구 기한 계산 |
| `taxrefund/discord_client.py` | 디스코드 임베드 생성 및 전송 (봇 토큰 REST, 웹훅 보조) |
| `taxrefund/notifier.py` | 채널 발송 및 상태 판정 (발송/건너뜀/실패) |
| `taxrefund/serve.py` | 상시 실행 서버: 스케줄링, 로깅, 안전 종료, 설정 변화 감지 |
| `taxrefund/web.py` | 웹 대시보드 (표준 라이브러리만, 모바일 우선) |
| `taxrefund/netcheck.py` | 공인 IP 조회·변동 감지 |
| `taxrefund/store.py` | SQLite 중복 방지 + 백필용 알림 복원 |
| `taxrefund/pipeline.py` | 전체 흐름 (`run` / `backfill` / `check`) |

### 모델 설정

`config.yaml`의 `analyze` 블록에서 조정합니다.

```yaml
analyze:
  model: claude-opus-5
  effort: high              # 비용을 줄이려면 medium 또는 low
  server_side_fallback: true
```

- **구조화 출력**으로 응답을 JSON 스키마에 고정해 파싱 실패를 없앴습니다.
- **프롬프트 캐싱**으로 판례마다 반복되는 시스템 프롬프트를 재사용합니다(입력 비용 절감).
- **서버사이드 폴백**(`fallbacks: "default"`)을 켜 두었습니다. 안전 분류기가 요청을 거절하면
  같은 호출 안에서 다른 모델이 이어받습니다. 계정에서 이 베타를 못 쓰면 표준 엔드포인트로
  자동 강등되므로 실패하지 않습니다. 원치 않으면 `server_side_fallback: false`.

## 17. 알아둘 것

- **수집 범위**: 국가법령정보센터에 실린 법원 판례와 기재부·국세청 법령해석례입니다.
  **조세심판원 심판결정례와 국세청 심사청구 결정은 이 API로 제공되지 않아 빠져 있습니다.**
  실무상 경정청구 근거로 중요한 자료이므로, 필요하면 국세법령정보시스템(`taxlaw.nts.go.kr`)을
  별도 수집원으로 붙여야 합니다. `taxrefund/lawgo.py`와 같은 인터페이스(`collect()`가
  `Precedent`를 내보내는 형태)로 모듈을 추가하면 나머지 파이프라인은 그대로 씁니다.
- **판례 반영 시차**: 선고 직후 바로 등재되지 않을 수 있어 `since_days`를 너무 짧게 잡으면
  놓칠 수 있습니다. 기본값 30일을 권장합니다.
- **기한 계산은 12월 결산 법인 기준**입니다. 결산월이 다르면 그대로 적용되지 않습니다.
- LLM 판단에는 오차가 있습니다. `min_confidence`를 낮추면 후보가 늘지만 오탐도 늘어납니다.
  처음에는 `--dry-run`으로 며칠 돌려 보며 문턱값을 조정하는 것을 권합니다.

---

## 웹에 공개된 스냅샷 (GitHub Pages)

<https://2gilhan.github.io/taxrefund/>

대시보드를 정적 HTML로 내보내 `docs/`에 넣고 GitHub Pages로 게시합니다.
공인 IP·Tailscale 주소·접근 토큰·국가법령 OC 값은 넣지 않고, 판례 원문 링크는
국가법령정보센터 열람 주소로 바꿉니다. 갱신하려면:

```powershell
python tools/export_static.py
git add docs && git commit -m "스냅샷 갱신" && git push
```
