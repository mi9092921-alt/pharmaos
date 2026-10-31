# PharmaOS VM-round UIA driver (elevated).
# File-based RPC: watches rpc/cmd.json, executes one action, writes rpc/result.json.
# Evidence screenshots go to the round's evidence folder.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$Base   = 'D:\projects\pharmaos\evidence\vm-round-2026-10-29'
$Rpc    = Join-Path $Base 'driver\rpc'
$Shots  = Join-Path $Base 'screenshots'
$Log    = Join-Path $Base 'driver\driver.log'
New-Item -ItemType Directory -Force -Path $Rpc, $Shots | Out-Null

function Log($m) { Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $m) }

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Rect {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

function Find-WizardWindow {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
  $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $wins) {
    $n = $w.Current.Name
    if ($n -match 'PharmaOS') { return $w }
  }
  return $null
}

function Shot-Window($el, $tag) {
  try {
    $h = [IntPtr]$el.Current.NativeWindowHandle
    $r = New-Object Win32Rect+RECT
    [Win32Rect]::GetWindowRect($h, [ref]$r) | Out-Null
    $wd = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
    if ($wd -le 0 -or $ht -le 0) { return $null }
    $bmp = New-Object System.Drawing.Bitmap($wd, $ht)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
    $g.Dispose()
    $p = Join-Path $Shots ("{0}-{1}.png" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $tag)
    $bmp.Save($p, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return $p
  } catch { Log ("shot error: " + $_.Exception.Message); return $null }
}

function Enum-Window($el) {
  $out = @()
  $cond = [System.Windows.Automation.Condition]::TrueCondition
  $els = $el.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  foreach ($e in $els) {
    $c = $e.Current
    $out += [pscustomobject]@{
      runtimeId = ($e.GetRuntimeId() -join ',')
      type = $c.ControlType.ProgrammaticName -replace '^ControlType\.',''
      name = $c.Name
      value = $c.Value
      enabled = $c.IsEnabled
      offscreen = $c.IsOffscreen
    }
  }
  return $out
}

function Find-Element($el, $name, $type) {
  $conds = @()
  if ($type) {
    $conds += New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::$type)
  }
  if ($name) {
    $conds += New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $name)
  }
  if ($conds.Count -eq 0) { return $null }
  $and = $conds[0]
  for ($i=1; $i -lt $conds.Count; $i++) { $and = New-Object System.Windows.Automation.AndCondition($and, $conds[$i]) }
  return $el.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $and)
}

Log "driver started (pid $PID, elevated: $([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator'))"

while ($true) {
  $cmdPath = Join-Path $Rpc 'cmd.json'
  if (Test-Path $cmdPath) {
    $cmd = $null
    $result = [ordered]@{ ok = $false; action = $null; error = $null; data = $null; screenshot = $null; at = (Get-Date -Format 'o') }
    try {
      $raw = Get-Content $cmdPath -Raw
      Remove-Item $cmdPath -Force
      $cmd = $raw | ConvertFrom-Json
      $result.action = $cmd.action
      Log ("cmd: " + $raw)
      switch ($cmd.action) {
        'ping'   { $result.ok = $true; $result.data = "driver-alive pid=$PID" }
        'enum'   {
          $w = Find-WizardWindow
          if ($w) {
            [Win32Rect]::SetForegroundWindow([IntPtr]$w.Current.NativeWindowHandle) | Out-Null
            Start-Sleep -Milliseconds 300
            $result.ok = $true
            $result.data = Enum-Window $w
            $result.screenshot = Shot-Window $w $cmd.tag
          } else { $result.error = 'PHARMAOS-WINDOW-NOT-FOUND' }
        }
        'click'  {
          $w = Find-WizardWindow
          if ($w) {
            $e = Find-Element $w $cmd.name $cmd.type
            if ($e) {
              $inv = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
              $inv.Invoke()
              Start-Sleep -Milliseconds 800
              $result.ok = $true
              $result.data = ("clicked name='" + $cmd.name + "' type='" + $cmd.type + "'")
              $result.screenshot = Shot-Window $w $cmd.tag
            } else { $result.error = "ELEMENT-NOT-FOUND name='$($cmd.name)' type='$($cmd.type)'" }
          } else { $result.error = 'PHARMAOS-WINDOW-NOT-FOUND' }
        }
        'settext' {
          $w = Find-WizardWindow
          if ($w) {
            $e = Find-Element $w $cmd.name $cmd.type
            if ($e) {
              $vp = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
              $vp.SetValue($cmd.text)
              Start-Sleep -Milliseconds 400
              $result.ok = $true
              $result.data = ("settext -> '" + $cmd.text + "'")
            } else { $result.error = "ELEMENT-NOT-FOUND name='$($cmd.name)' type='$($cmd.type)'" }
          } else { $result.error = 'PHARMAOS-WINDOW-NOT-FOUND' }
        }
        'key' {
          [System.Windows.Forms.SendKeys]::SendWait($cmd.text)
          Start-Sleep -Milliseconds 500
          $result.ok = $true
          $w = Find-WizardWindow
          if ($w) { $result.screenshot = Shot-Window $w $cmd.tag }
        }
        'screenshot' {
          $w = Find-WizardWindow
          if ($w) { $result.ok = $true; $result.screenshot = Shot-Window $w $cmd.tag }
          else { $result.error = 'PHARMAOS-WINDOW-NOT-FOUND' }
        }
        'waithidden' {
          # wait until no PharmaOS-Setup process remains (installer finished)
          $deadline = (Get-Date).AddSeconds([int]$cmd.timeoutSec)
          while ((Get-Date) -lt $deadline) {
            $p = Get-Process | Where-Object { $_.Name -like 'PharmaOS-Setup*' }
            if (-not $p) { break }
            Start-Sleep -Milliseconds 800
          }
          $left = Get-Process | Where-Object { $_.Name -like 'PharmaOS-Setup*' }
          $result.ok = $true
          $result.data = @{ setupGone = (-not $left) }
        }
        'launch-non-elevated' {
          # launch app via explorer delegate so it runs with the normal user token
          Start-Process explorer.exe -ArgumentList ('"' + $cmd.path + '"')
          Start-Sleep -Seconds 4
          $p = Get-Process | Where-Object { $_.Name -like 'PharmaOS*' -and $_.Name -notlike 'PharmaOS-Setup*' } | Select-Object -First 1
          $result.ok = [bool]$p
          $result.data = @{ appPid = if ($p) { $p.Id } else { $null }; name = if ($p) { $p.Name } else { $null } }
        }
        default { $result.error = "UNKNOWN-ACTION $($cmd.action)" }
      }
    } catch {
      $result.error = $_.Exception.Message
      Log ("error: " + $_.Exception.Message)
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $Rpc 'result.json') -Encoding UTF8
    Log ("result written: ok=" + $result.ok + " err=" + $result.error)
  }
  Start-Sleep -Milliseconds 250
}
