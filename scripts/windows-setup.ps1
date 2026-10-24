# ============================================================================
#  BidPilot — راه‌اندازی روی ویندوز بدون دردسر pip
#  اجرا در PowerShell، از ریشه‌ی مخزن:
#     powershell -ExecutionPolicy Bypass -File scripts\windows-setup.ps1
# ============================================================================
$ErrorActionPreference = "Stop"

Write-Host "== 1) بررسی پایتون ==" -ForegroundColor Cyan
$python = $null
foreach ($candidate in @("py", "python")) {
    try {
        $v = & $candidate -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.10") { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Write-Host "پایتون ۳.۱۰+ پیدا نشد. از https://www.python.org/downloads/windows/ نصب کن (نه از Microsoft Store)." -ForegroundColor Red
    exit 1
}
& $python -VV

Write-Host "== 2) بررسی سلامت runpy (منشأ خطای null bytes) ==" -ForegroundColor Cyan
& $python -c "import runpy; print('runpy OK:', runpy.__file__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "نصب پایتون خراب است. آن را حذف و از python.org دوباره نصب کن، سپس این اسکریپت را دوباره اجرا کن." -ForegroundColor Red
    exit 1
}

Write-Host "== 3) ساخت محیط مجازی ==" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { & $python -m venv .venv }
$venvPython = ".\.venv\Scripts\python.exe"
& $venvPython -m ensurepip --upgrade | Out-Null
& $venvPython -m pip install --upgrade pip setuptools wheel

Write-Host "== 4) نصب BidPilot (editable + dev) ==" -ForegroundColor Cyan
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "نصب با pip ناموفق بود؛ حالت جایگزین: بدون نصب و با PYTHONPATH اجرا کن:" -ForegroundColor Yellow
    Write-Host '  $env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m bidpilot setup'
    exit 1
}

Write-Host "== 5) بسته‌های اختیاری (pypdf برای خواندن PDF رزومه) ==" -ForegroundColor Cyan
& $venvPython -m bidpilot deps --install
if ($LASTEXITCODE -ne 0) {
    Write-Host "نصب pypdf ناموفق بود؛ بعداً با /deps در تلگرام یا «bidpilot deps --install» امتحان کن." -ForegroundColor Yellow
}

Write-Host "== 6) تست سلامت ==" -ForegroundColor Cyan
& $venvPython -m bidpilot version
& $venvPython -m bidpilot selftest --offline
Write-Host "`nآماده است:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\activate"
Write-Host "  bidpilot setup"
Write-Host "  bidpilot selftest"
Write-Host ""
Write-Host "همیشه ربات را با همین پایتون اجرا کن (نه «python» یا «bidpilot» سراسری):" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\python.exe -m bidpilot bot --run"
