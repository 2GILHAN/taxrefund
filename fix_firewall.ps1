# =====================================================================
#  대시보드 접속용 방화벽 규칙 추가  (★ 관리자 권한 필요 ★)
#
#  왜 필요한가:
#    Tailscale 인터페이스는 Windows 방화벽에서 'Private' 프로필로 잡힙니다.
#    그런데 Python에 자동 생성된 인바운드 허용 규칙은 'Public' 프로필만
#    덮고 있어서, 폰에서 Tailscale로 들어오는 연결이 차단됩니다.
#    (PC에서 자기 IP로 접속하는 건 루프백이라 방화벽을 거치지 않아 잘 됩니다.
#     그래서 "PC에선 되는데 폰에선 안 되는" 상황이 생깁니다.)
#
#  실행 방법:
#    시작 > 'PowerShell' 검색 > 마우스 우클릭 > "관리자 권한으로 실행"
#    그 창에서 아래를 붙여넣기:
#
#      powershell -ExecutionPolicy Bypass -File c:\_project\taxrefund\fix_firewall.ps1
#
#  해제:  ... -File fix_firewall.ps1 -Remove
# =====================================================================

param([switch]$Remove, [int]$Port = 8787)

$RuleName = "TaxRefundBot Dashboard"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "이 스크립트는 관리자 권한이 필요합니다." -ForegroundColor Yellow
    Write-Host "시작 > PowerShell 우클릭 > '관리자 권한으로 실행' 후 다시 실행하세요."
    exit 1
}

if ($Remove) {
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Write-Host "방화벽 규칙 '$RuleName' 을 제거했습니다."
    exit 0
}

Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue

# Tailscale(Private 프로필)로 들어오는 8787만 허용합니다.
# 공인 IP가 붙은 이더넷(Public)은 열지 않으므로 인터넷에 노출되지 않습니다.
New-NetFirewallRule `
    -DisplayName $RuleName `
    -Description "법인세 판례 봇 대시보드 - Tailscale 경유 접속 허용" `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -Profile Private `
    -RemoteAddress 100.64.0.0/10 | Out-Null

Write-Host "방화벽 규칙 추가 완료" -ForegroundColor Green
Write-Host "  이름     : $RuleName"
Write-Host "  포트     : TCP $Port (인바운드)"
Write-Host "  프로필   : Private (Tailscale)"
Write-Host "  허용 대상: 100.64.0.0/10 (Tailscale 대역만)"
Write-Host ""
Write-Host "이제 폰에서 접속해 보세요:"
Write-Host "  http://100.106.34.95:$Port?t=<WEB_TOKEN>"
