# Сборка DVHectare.exe для Windows 7 / 8 / 8.1 (работает и на 10/11).
#
# Официальный Python для Windows 7 закончился на 3.8, а библиотеки программы требуют 3.10+.
# Поэтому сборка идёт на неофициальной сборке Python для Windows 7 (github.com/adang1345/PythonWin7).
# Собирать можно на обычном компьютере с Windows 10/11 — готовый exe переносится на Windows 7.
#
# 1. Скачайте и установите Python 3.12 x64 со страницы https://github.com/adang1345/PythonWin7
#    (например, в C:\Python312-win7, галочку «Add to PATH» НЕ ставить).
# 2. Запустите:  .\build_exe_win7.ps1 -Python C:\Python312-win7\python.exe
# 3. Перенесите папку dist-win7 на компьютер с Windows 7 и запустите там DVHectare.exe --selftest check.txt
#
# На Windows 7 интерфейс открывается в браузере (Chrome 109 / Firefox ESR 115 — последние для Win7):
# встроенного окна WebView2 там нет.
param([Parameter(Mandatory = $true)][string]$Python)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not (Test-Path $Python)) { throw "Не найден $Python — укажите python.exe из сборки PythonWin7" }
& $Python -c "import sys; assert sys.version_info >= (3, 10), sys.version; print('Python', sys.version)"
if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.10+ из сборки PythonWin7" }

$venv = ".venv-win7"
if (-not (Test-Path "$venv\Scripts\python.exe")) { & $Python -m venv $venv }
$py = "$venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install --upgrade -e ".[geo,agent]" "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой" }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name DVHectare `
    --distpath dist-win7 --workpath build-win7 `
    --collect-all pyproj --collect-all shapely --collect-all folium --collect-all branca --collect-all xyzservices `
    --collect-submodules google.genai --collect-data certifi `
    --add-data "dvhectare\gui_static;dvhectare\gui_static" `
    --exclude-module webview --exclude-module clr --exclude-module pythonnet `
    --exclude-module geopandas --exclude-module pyogrio --exclude-module matplotlib --exclude-module pytest `
    dvhectare_gui.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

if (Test-Path "config.yaml") { Copy-Item "config.yaml" "dist-win7\config.yaml" -Force }
else { Copy-Item "config.example.yaml" "dist-win7\config.yaml" -Force }
if (Test-Path "certs") { Copy-Item "certs" "dist-win7\certs" -Recurse -Force }

# На Windows 7 окна WebView2 нет — сразу режим браузера
$cfg = Get-Content "dist-win7\config.yaml" -Raw -Encoding UTF8
if ($cfg -notmatch "(?m)^gui:") { $cfg += "`r`ngui:`r`n  mode: browser`r`n" }
else { $cfg = $cfg -replace "(?m)^(\s+mode:\s*)\S+", '${1}browser' }
[IO.File]::WriteAllText((Resolve-Path "dist-win7\config.yaml"), $cfg, (New-Object Text.UTF8Encoding $false))

& "dist-win7\DVHectare.exe" --selftest "dist-win7\selftest.txt" | Out-Null
Start-Sleep -Seconds 2
Get-Content "dist-win7\selftest.txt" -Encoding UTF8
Remove-Item "dist-win7\selftest.txt", "dist-win7\selftest.html" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Готово: dist-win7\DVHectare.exe — перенесите всю папку dist-win7 на Windows 7." -ForegroundColor Green
Write-Host "На Windows 7 нужны обновления KB3063858 (или KB2533623) и KB2999226 (Universal C Runtime)." -ForegroundColor Yellow
