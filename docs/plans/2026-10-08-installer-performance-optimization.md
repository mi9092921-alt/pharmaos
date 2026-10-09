# Installer Performance Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Drastically reduce the installation duration of `PharmaOS-Setup-*.exe` from several minutes to under 30 seconds by fixing Windows Defender exclusion timing, pruning over 10,000 non-runtime files from the web bundle, and optimizing extraction.

**Architecture:** Optimize the offline NSIS installer pipeline across three layers:

1. Fix the NSIS lifecycle in `installer.nsh` so Windows Defender real-time scanning exclusions are applied _before_ file extraction starts, and run post-install steps in `customInstall`.
2. Clean and prune non-runtime files (`*.map`, `*.d.ts`, `*.ts`, `*.md`, test directories) from `dist/web` in `build-web.ps1`, reducing the extracted file count by ~40-50%.
3. Package the remaining web standalone tree as a bundled archive or tune NSIS compression to avoid the single-threaded GUI extraction bottleneck on 25,000+ loose files.

**Tech Stack:** NSIS (Nullsoft Scriptable Install System), Electron-Builder, PowerShell 7/5.1, Windows Defender (MpPreference), Next.js Standalone, bsdtar.

---

### Task 1: Fix NSIS Lifecycle & Windows Defender Exclusion Timing

**Files:**

- Modify: `installer/installer.nsh:1-14`
- Modify: `installer/install-steps.ps1:1-36`

**Step 1: Inspect the current defect in `installer.nsh`**

- Currently, `installer.nsh` calls `"$INSTDIR\resources\install-steps.ps1"` in `!macro preInit`.
- In a fresh installation, `$INSTDIR` has not been extracted yet, causing `install-steps.ps1` to fail silently.
- As a result, `Add-MpPreference -ExclusionPath` never executes before files are written, leaving Windows Defender to inspect all 28,000+ files in real-time during extraction.

**Step 2: Update `installer/installer.nsh`**

- In `preInit`: Run an inline PowerShell command to:
  1. Kill any existing running instances (`pharmaos-api`, `node`).
  2. Add Windows Defender exclusions for `$PROGRAMFILES\PharmaOS` and `$PROGRAMDATA\PharmaOS` _before_ the installer starts writing files to disk.
- In `customInstall`: Call `"$INSTDIR\resources\install-steps.ps1"` _after_ all files have been copied to `$INSTDIR`.

```nsis
; installer/installer.nsh - Optimized NSIS lifecycle
!macro preInit
  ; Pre-install: Kill old processes and add Defender exclusions BEFORE file extraction
  nsExec::Exec 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Stop-Process -Name pharmaos-api,node -Force -ErrorAction SilentlyContinue; Add-MpPreference -ExclusionPath ''$PROGRAMFILES\PharmaOS'',''$PROGRAMDATA\PharmaOS'' -ErrorAction SilentlyContinue"'
!macroend

!macro customInstall
  ; Post-install: Run directory creation, ACL permissions, and service setups
  nsExec::Exec 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\resources\install-steps.ps1"'
!macroend

!macro customUnInstall
  nsExec::Exec 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall-steps.ps1"'
!macroend
```

**Step 3: Refactor `installer/install-steps.ps1`**

- Keep `install-steps.ps1` focused on post-extraction operations (directories `backups` and `logs`, setting ACLs, and verifying permissions).
- Make sure ACL commands run efficiently without hanging.

**Step 4: Verify syntax and behavior**

- Test command:
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File installer/install-steps.ps1
  ```
- Expected output: Exits cleanly with return code 0.

---

### Task 2: Prune Non-Runtime Files from `installer/dist/web` in `build-web.ps1`

**Files:**

- Modify: `installer/build-web.ps1`

**Step 1: Identify file bloat in `installer/dist/web`**

- Currently, `dist/web` contains ~25,749 files.
- Over 10,000 files are sourcemaps (`*.map`), TypeScript declarations (`*.d.ts`), TypeScript source files (`*.ts`), Markdown documentation (`*.md`), license texts, and test fixtures (`__tests__`, `test`).
- None of these are required by Node.js to execute `server.js`.

**Step 2: Add an automated pruning stage to `build-web.ps1`**

- In `installer/build-web.ps1`, immediately after the robocopy stages (after line 78), add a cleanup step:

```powershell
Write-Host "pruning non-runtime files (*.map, *.d.ts, *.md, tests) from dist/web..."
$extensionsToDelete = @("*.map", "*.d.ts", "*.tsbuildinfo", "*.md", "*.markdown")
foreach ($ext in $extensionsToDelete) {
    Get-ChildItem -Path $Out -Recurse -Filter $ext -File | Remove-Item -Force
}

# Remove test directories and documentation inside node_modules
Get-ChildItem -Path $Out -Recurse -Directory | Where-Object {
    $_.Name -in @("__tests__", "test", "tests", "docs", "example", "examples")
} | Remove-Item -Recurse -Force
```

**Step 3: Run `build-web.ps1` and verify reduction**

- Run:
  ```powershell
  powershell -ExecutionPolicy Bypass -File installer/build-web.ps1
  ```
- Measure file count:
  ```powershell
  $count = (Get-ChildItem -Path "installer\dist\web" -Recurse -File).Count
  Write-Host "New web file count: $count"
  ```
- Expected result: File count drops from ~25,749 to ~13,000-15,000 files (reduction of >10,000 files).

---

### Task 3: Bundle Web Standalone as an Archive (`web.tar`) for Fast Stream Extraction

**Files:**

- Modify: `installer/build-web.ps1`
- Modify: `apps/desktop/electron-builder.yml`
- Modify: `installer/installer.nsh`

**Step 1: Evaluate the Archive Extraction Strategy**

- NSIS GUI extracts files individually on the UI thread, updating the progress bar and UI for every single file. Extracting 15,000-25,000 files one-by-one is the fundamental cause of the slow install bar.
- Windows 10 and 11 include native `tar.exe` (`bsdtar`) in `System32\tar.exe`.
- Extracting a single `web.tar` using `tar -xf` takes ~2-4 seconds because it streams data continuously without per-file NSIS GUI message overhead.

**Step 2: Archive `apps/web` into `web.tar` in `build-web.ps1`**

- Instead of leaving tens of thousands of loose files in `extraResources`:
  - `build-web.ps1` creates `installer/dist/web.tar` (or keeps `node.exe` outside and archives the `apps` and `node_modules` folders).
- In `electron-builder.yml`:
  - Include `web.tar` and `node.exe` in `extraResources` instead of the 25,000 loose files.
- In `installer.nsh` (`customInstall`):
  - Run:
    ```nsis
    nsExec::Exec 'tar.exe -xf "$INSTDIR\resources\web.tar" -C "$INSTDIR\resources\web"'
    Delete "$INSTDIR\resources\web.tar"
    ```

**Step 3: Verify fallback and standalone execution**

- Verify that `apps/desktop/src/main.ts` correctly finds `resources/web/apps/web/server.js` and launches `node.exe`.
- Test running `smoke-test.ps1` to ensure the runtime operates identically.

---

### Task 4: Tune Compression in `electron-builder.yml`

**Files:**

- Modify: `apps/desktop/electron-builder.yml`

**Step 1: Check compression setting**

- Currently, `electron-builder.yml` uses default NSIS compression (LZMA2 `maximum`), which maximizes compression ratio at the expense of high decompression CPU time.
- Set `compression: normal` under `nsis` or at top level to balance installer size and decompression speed if archive extraction is not used, or keep optimal ratio if `web.tar` is used.

**Step 2: Test installer generation**

- Run:
  ```powershell
  Push-Location apps/desktop; npx electron-builder --win nsis; Pop-Location
  ```
- Check setup executable size.

---

### Task 5: End-to-End Build, Benchmark & Verification

**Files:**

- Test: `installer/build-installer.ps1`
- Test: `installer/smoke-test.ps1`

**Step 1: Build the complete installer**

- Run:
  ```powershell
  powershell -ExecutionPolicy Bypass -File installer/build-installer.ps1
  ```
- Verify: Exit code 0, installer generated at `installer/dist/installer/PharmaOS-Setup-*.exe`.

**Step 2: Benchmark installation duration**

- Measure installation time before vs. after:
  - Previous duration: Several minutes.
  - Target duration: < 30-45 seconds.
- Verify Defender exclusion takes effect immediately.

**Step 3: Run the full smoke test**

- Run:
  ```powershell
  powershell -ExecutionPolicy Bypass -File installer/smoke-test.ps1 -Clean
  ```
- Expected result: 100% of checks pass (DB initialization, migration, API health, backup, restore).
