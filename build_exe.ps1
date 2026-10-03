# Сборка DVHectare.exe (окно: ключ и модель Gemini, запрос, «Искать», карта в окне).
# Запуск:  .\build_exe.ps1
# Результат: папка dist\ — DVHectare.exe + config.yaml + certs\. Её можно переносить целиком.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
Write-Host "Python: $py" -ForegroundColor Cyan

& $py -m pip install --upgrade -e ".[geo,agent,gui]" "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой" }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name DVHectare `
    --collect-all pyproj --collect-all shapely --collect-all folium --collect-all branca --collect-all xyzservices `
    --collect-submodules google.genai --collect-data certifi `
    --collect-all webview --hidden-import clr `
    --add-data "dvhectare\gui_static;dvhectare\gui_static" `
    --exclude-module geopandas --exclude-module pyogrio --exclude-module matplotlib --exclude-module pytest `
    dvhectare_gui.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

# Рядом с exe кладём настройки и сертификат — программа читает их из своей папки
if (Test-Path "config.yaml") { Copy-Item "config.yaml" "dist\config.yaml" -Force }
else { Copy-Item "config.example.yaml" "dist\config.yaml" -Force }
if (Test-Path "certs") { Copy-Item "certs" "dist\certs" -Recurse -Force }

# Самопроверка: все библиотеки и данные (pyproj, folium, pywebview, интерфейс) упакованы
& "dist\DVHectare.exe" --selftest "dist\selftest.txt" | Out-Null
Start-Sleep -Seconds 1
Get-Content "dist\selftest.txt"
Remove-Item "dist\selftest.txt", "dist\selftest.html" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Готово: dist\DVHectare.exe" -ForegroundColor Green
Write-Host "Ключ Gemini можно ввести прямо в окне программы (сохранится в dist\config.yaml)." -ForegroundColor Green
Write-Host "Нужен Microsoft Edge WebView2 Runtime — в Windows 10/11 он обычно уже установлен." -ForegroundColor Green
