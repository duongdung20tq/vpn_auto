# Build VpnAuto.exe (VPN) + RdpAuto.exe (RDP retry) and VpnAuto-release.zip.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "unit tests failed" }

$common = @(
    "--hidden-import", "vpn_auto.cisco", "--hidden-import", "vpn_auto.cisco_watchdog",
    "--hidden-import", "vpn_auto.pulse", "--hidden-import", "vpn_auto.watchdog",
    "--hidden-import", "vpn_auto.prehook", "--hidden-import", "vpn_auto.rdp",
    "--hidden-import", "vpn_auto.rdp_ui", "--hidden-import", "vpn_auto.rdp_store"
)

python -m PyInstaller --noconfirm --clean --onefile --noconsole --name VpnAuto `
    --paths . --hidden-import pystray._win32 @common vpn_auto\__main__.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller VpnAuto failed" }

python -m PyInstaller --noconfirm --clean --onefile --noconsole --name RdpAuto `
    --paths . @common vpn_auto\rdp_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller RdpAuto failed" }

$release = "dist\release"
Remove-Item $release -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $release | Out-Null
Copy-Item dist\VpnAuto.exe, dist\RdpAuto.exe, vpn_auto.example.ini, README.md $release
Compress-Archive -Path "$release\*" -DestinationPath dist\VpnAuto-release.zip -Force
Get-Item dist\VpnAuto.exe, dist\RdpAuto.exe, dist\VpnAuto-release.zip | Select-Object Name, Length, LastWriteTime
