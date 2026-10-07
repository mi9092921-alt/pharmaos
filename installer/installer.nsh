; installer/installer.nsh - M5. All real logic lives in install-steps.ps1 /
; uninstall-steps.ps1 (shipped as resources): makensis hard-crashes on some
; quoted constructs, so NSIS only launches the PowerShell helpers.
!macro preInit
  nsExec::Exec 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\install-steps.ps1"'
!macroend

!macro customInstall
!macroend

!macro customUnInstall
  nsExec::Exec 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall-steps.ps1"'
!macroend
