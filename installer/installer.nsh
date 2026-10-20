; installer/installer.nsh - M5. All real logic lives in install-steps.ps1 /
; uninstall-steps.ps1 (shipped as resources): makensis hard-crashes on some
; quoted constructs, so NSIS only launches the PowerShell helpers.
;
; IMPORTANT: run the install helper from customInstall, not preInit. preInit
; runs before NSIS has copied extraResources and before $INSTDIR is finalized,
; so the old hook silently did nothing. That left ProgramData without the
; user-writable logs/backups directories and the launcher exited before its
; splash window could be displayed.
!macro customInstall
  nsExec::Exec 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\install-steps.ps1"'
!macroend

!macro customUnInstall
  nsExec::Exec 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall-steps.ps1"'
!macroend
