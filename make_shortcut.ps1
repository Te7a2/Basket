# Creates the AFAQ desktop shortcut. It opens the vouchers program in Google Chrome.
# Usage:  make_shortcut.ps1 -Url "http://localhost:8501"      (create / refresh)
#         make_shortcut.ps1 -Remove                             (delete the shortcut)
param(
    [string]$Url = "http://localhost:8501",
    [string]$Name = "سندات أفاق",
    [switch]$Remove
)
$ErrorActionPreference = "Continue"

# Desktop of the current user + the shared desktop (all users, needs administrator rights)
$desktops = @()
$desktops += [Environment]::GetFolderPath("Desktop")
$desktops += [Environment]::GetFolderPath("CommonDesktopDirectory")
$desktops = $desktops | Where-Object { $_ } | Select-Object -Unique

if ($Remove) {
    foreach ($d in $desktops) {
        foreach ($ext in "lnk", "url") {
            $f = Join-Path $d "$Name.$ext"
            if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
        }
    }
    Write-Host "Desktop shortcut removed."
    exit 0
}

# Keep the icon in a stable folder so the shortcut keeps working if the install folder moves
$iconSrc = Join-Path $PSScriptRoot "afaq.ico"
$iconDir = Join-Path $env:ProgramData "AfaqVouchers"
try {
    New-Item -ItemType Directory -Force -Path $iconDir -ErrorAction Stop | Out-Null
} catch {
    $iconDir = Join-Path $env:LOCALAPPDATA "AfaqVouchers"
    New-Item -ItemType Directory -Force -Path $iconDir | Out-Null
}
$icon = Join-Path $iconDir "afaq.ico"
if (Test-Path -LiteralPath $iconSrc) { Copy-Item -LiteralPath $iconSrc -Destination $icon -Force }

# Find Google Chrome
$cands = @()
foreach ($v in "ProgramFiles", "ProgramFiles(x86)", "LocalAppData") {
    $b = [Environment]::GetEnvironmentVariable($v)
    if ($b) { $cands += (Join-Path $b "Google\Chrome\Application\chrome.exe") }
}
foreach ($k in "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
               "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") {
    try {
        $p = (Get-ItemProperty -Path $k -ErrorAction Stop).'(default)'
        if ($p) { $cands += $p }
    } catch { }
}
$chrome = $cands | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

$ws = New-Object -ComObject WScript.Shell
$created = 0
foreach ($d in $desktops) {
    try {
        if ($chrome) {
            $lnk = $ws.CreateShortcut((Join-Path $d "$Name.lnk"))
            $lnk.TargetPath = $chrome
            $lnk.Arguments = $Url
            $lnk.WorkingDirectory = Split-Path -Parent $chrome
            $lnk.Description = "AFAQ vouchers"
            if (Test-Path -LiteralPath $icon) { $lnk.IconLocation = "$icon,0" }
            $lnk.Save()
        } else {
            $content = "[InternetShortcut]`r`nURL=$Url`r`nIconFile=$icon`r`nIconIndex=0`r`n"
            Set-Content -LiteralPath (Join-Path $d "$Name.url") -Value $content -Encoding ASCII
        }
        $created++
    } catch { }
}

if ($created -eq 0) {
    Write-Host "Could not create the shortcut (no permission on the desktop)."
    exit 1
}
if ($chrome) {
    Write-Host "Desktop shortcut created. It opens in Google Chrome: $Url"
} else {
    Write-Host "Google Chrome was not found. A shortcut that uses the default browser was created instead."
}
exit 0
