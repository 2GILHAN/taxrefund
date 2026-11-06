# =====================================================================
#  법인세 판례 봇 - 로그온 자동 시작 등록 (관리자 권한 불필요)
#
#  시작프로그램 폴더에 pythonw.exe를 직접 가리키는 바로가기를 만듭니다.
#  중간에 스크립트 호스트를 거치지 않아 가장 단순하고 확실합니다.
#  pythonw.exe는 콘솔 창이 없어 로그온 시 창이 뜨지 않습니다.
#
#  등록:  powershell -ExecutionPolicy Bypass -File install_startup.ps1
#  해제:  powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
# =====================================================================

param([switch]$Remove)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Startup = [Environment]::GetFolderPath('Startup')
$LinkPath = Join-Path $Startup "TaxRefundBot.lnk"
$LegacyVbs = Join-Path $Startup "TaxRefundBot.vbs"

# 예전 방식(VBS)이 남아 있으면 정리합니다.
if (Test-Path $LegacyVbs) { Remove-Item $LegacyVbs -Force }

if ($Remove) {
    if (Test-Path $LinkPath) { Remove-Item $LinkPath -Force }
    Write-Host "자동 시작 등록을 해제했습니다."
    return
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Error "pythonw.exe를 찾을 수 없습니다. Python이 PATH에 있는지 확인하세요."
    return
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($LinkPath)
$link.TargetPath = $pythonw
$link.Arguments = "-m taxrefund serve"
$link.WorkingDirectory = $ProjectDir
$link.WindowStyle = 7            # 최소화
$link.Description = "법인세 판례 -> 경정청구 알림 봇"
$link.Save()

Write-Host "자동 시작 등록 완료"
Write-Host "  바로가기 : $LinkPath"
Write-Host "  실행     : $pythonw -m taxrefund serve"
Write-Host "  시작 위치: $ProjectDir"
Write-Host ""
Write-Host "이제 로그온할 때마다 봇이 자동으로 뜹니다."
Write-Host "봇은 config.yaml의 serve.daily_at(기본 08:00)에 맞춰 매일 실행됩니다."
