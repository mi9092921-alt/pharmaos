param(
  [int]$Seconds = 0,
  [string]$Tag = 'shot'
)
# Screenshot helper for the PharmaOS round.
# - Selects the process by EXECUTABLE PATH (not window title).
# - With -Seconds N: polls the foreground window; captures only when the
#   PharmaOS app is focused, so operator's wizard screens are documented
#   as they happen without capturing other windows.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
$Shots = 'D:\projects\pharmaos\evidence\vm-round-2026-10-29\screenshots'
New-Item -ItemType Directory -Force -Path $Shots | Out-Null

function Get-AppProcess {
  Get-Process | Where-Object {
    $_.Path -like 'D:\Program Files\PharmaOS\PharmaOS.exe' -and $_.MainWindowHandle -ne 0
  } | Sort-Object StartTime | Select-Object -First 1
}

function Shot-Hwnd([IntPtr]$h, [string]$tag) {
  $r = New-Object W32+RECT
  [W32]::GetWindowRect($h, [ref]$r) | Out-Null
  $wd = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
  if ($wd -le 0 -or $ht -le 0) { return $null }
  $bmp = New-Object System.Drawing.Bitmap($wd, $ht)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
  $g.Dispose()
  $out = Join-Path $Shots ("{0}-{1}.png" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $tag)
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  return $out
}

if ($Seconds -le 0) {
  $p = Get-AppProcess
  if (-not $p) { Write-Output "NO-APP-WINDOW"; exit 1 }
  $out = Shot-Hwnd $p.MainWindowHandle $Tag
  Write-Output ("pid=" + $p.Id + " saved=" + $out)
} else {
  $deadline = (Get-Date).AddSeconds($Seconds)
  $n = 0
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 1500
    $fg = [W32]::GetForegroundWindow()
    if ($fg -eq [IntPtr]::Zero) { continue }
    $fgpid = 0
    [W32]::GetWindowThreadProcessId($fg, [ref]$fgpid) | Out-Null
    $fp = Get-Process -Id $fgpid -ErrorAction SilentlyContinue
    if ($fp -and $fp.Path -like 'D:\Program Files\PharmaOS\PharmaOS.exe') {
      $n++
      $out = Shot-Hwnd $fg ("wizard-%03d" -f $n)
      Write-Output ("captured fg app window: " + $out)
    }
  }
  Write-Output "poll-done"
}
