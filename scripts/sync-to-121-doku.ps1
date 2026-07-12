# Lokaler Windows-Sync zwischen zwei Git-Repos (gleicher github_code-Ordner):
#
#   Repo 1 (öffentlich):  paperless-ngx-classifier  ->  github.com/.../paperless-ngx-classifier
#   Repo 2 (privat):      doku                        ->  github.com/.../doku
#                         └── pve2/vm/121-paperless/Doku/paperless-scripts/
#
# Source of truth für Code: paperless-ngx-classifier
# Dieses Skript kopiert geänderte Dateien in das doku-Repo (121-paperless Doku-Kopie).
# Danach im doku-Repo committen und pushen — NICHT automatisch.
#
# Nutzung (in PowerShell, aus paperless-ngx-classifier):
#   .\scripts\sync-to-121-doku.ps1
#   .\scripts\sync-to-121-doku.ps1 -WhatIf
#
# Typischer Ablauf nach Code-Änderung:
#   1. paperless-ngx-classifier: git add / commit / push
#   2. .\scripts\sync-to-121-doku.ps1
#   3. doku: git add pve2/vm/121-paperless/... / commit / push
#   4. ct-121 (Server): git pull && ./scripts/deploy-to-ct121.sh
#
# Überschreibt NICHT: fix_*.py, create_tags.py, paperless-backup.sh (nur lokal in 121-Doku)

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DstDokuRoot = ""
)

$ErrorActionPreference = "Stop"
$SrcRoot = Split-Path -Parent $PSScriptRoot

if (-not $DstDokuRoot) {
    $DstDokuRoot = Join-Path $SrcRoot "..\doku\pve2\vm\121-paperless\Doku" | Resolve-Path -ErrorAction SilentlyContinue
    if (-not $DstDokuRoot) {
        $DstDokuRoot = Join-Path $SrcRoot "..\doku\pve2\vm\121-paperless\Doku"
    }
}

$DstScripts = Join-Path $DstDokuRoot "paperless-scripts"
$DstDocs    = Join-Path $DstDokuRoot "docs"
$DstExamples = Join-Path $DstScripts "training\json_examples"

function Sync-File {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    if (-not (Test-Path $Source)) {
        Write-Warning "Quelle fehlt, übersprungen: $Source"
        return
    }
    $destDir = Split-Path -Parent $Destination
    if (-not (Test-Path $destDir)) {
        if ($PSCmdlet.ShouldProcess($destDir, "Verzeichnis anlegen")) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
    }
    $changed = $true
    if ((Test-Path $Destination) -and (Get-FileHash $Source).Hash -eq (Get-FileHash $Destination).Hash) {
        $changed = $false
    }
    $action = if ($changed) { "Kopieren" } else { "Unveraendert" }
    if ($PSCmdlet.ShouldProcess($Destination, $action)) {
        if ($changed) {
            Copy-Item -Path $Source -Destination $Destination -Force
            Write-Host "  -> $Destination" -ForegroundColor Green
        } else {
            Write-Host "  =  $Destination" -ForegroundColor DarkGray
        }
    }
}

Write-Host "Sync paperless-ngx-classifier -> 121-paperless Doku" -ForegroundColor Cyan
Write-Host "  Quelle: $SrcRoot"
Write-Host "  Ziel:   $DstDokuRoot"
Write-Host ""

Write-Host "Skripte:" -ForegroundColor Yellow
@(
    "correspondent_manager_app.py",
    "paper_manager_ui.html",
    "post_consume.py",
    "iban_utils.py",
    "pre_consume.sh",
    "pre_consume_qr.py",
    "requirements-corr-manager.txt"
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot $_) (Join-Path $DstScripts $_)
}
Sync-File (Join-Path $SrcRoot "scripts\deploy-to-ct121.sh") (Join-Path $DstScripts "deploy-to-ct121.sh")

Write-Host ""
Write-Host "Legacy-Skripte:" -ForegroundColor Yellow
@(
    "legacy-import-batch.sh",
    "legacy-migrate-all.sh",
    "legacy-one-batch.sh",
    "legacy-tasks-summary.sh",
    "legacy-duplicate-audit.sh",
    "legacy-nas-sha256.sh",
    "paperless-version-check.sh"
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot "scripts\$_") (Join-Path $DstScripts $_)
}

Write-Host ""
Write-Host "Training-Beispiele:" -ForegroundColor Yellow
@(
    "correspondents.example.json",
    "document_types.example.json",
    "document_review_queue.example.jsonl",
    "family.example.json",
    "manifest.example.json",
    "pending_correspondents.example.jsonl",
    "tags.example.json",
    "pending_mode.txt"
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot "training\$_") (Join-Path $DstExamples $_)
}

Write-Host ""
Write-Host "Doku:" -ForegroundColor Yellow
@(
    @{ Src = "README.md";           Dst = "README.md" },
    @{ Src = "README.de.md";       Dst = "README.de.md" },
    @{ Src = "INSTALL.md";          Dst = "INSTALL.md" },
    @{ Src = "docs\Benutzerhandbuch_paper_manager.md"; Dst = "Benutzerhandbuch_paper_manager.md" },
    @{ Src = "docs\Bedienungsanleitung_paper_manager.doc"; Dst = "Bedienungsanleitung_paper_manager.doc" },
    @{ Src = "docs\Bedienungsanleitung_paper_manager.docx"; Dst = "Bedienungsanleitung_paper_manager.docx" },
    @{ Src = "docs\DEVELOPER.md"; Dst = "DEVELOPER.md" },
    @{ Src = "docs\BRILLENPASS_HANDOFF.md"; Dst = "BRILLENPASS_HANDOFF.md" },
    @{ Src = "docs\VERSIONING.md"; Dst = "VERSIONING.md" },
    @{ Src = "docs\LEGACY_MIGRATION_PLAN.md"; Dst = "LEGACY_MIGRATION_PLAN.md" },
    @{ Src = "docs\LEGACY_IMPORT.md";         Dst = "LEGACY_IMPORT.md" },
    @{ Src = "docs\UPGRADE_V3.md";            Dst = "UPGRADE_V3.md" }
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot $_.Src) (Join-Path $DstDocs $_.Dst)
}

# Doku-Repo: relative Links (classifier zeigt oft auf doku-Pfad)
$legacyImport = Join-Path $DstDocs "LEGACY_IMPORT.md"
if (Test-Path $legacyImport) {
    $content = Get-Content $legacyImport -Raw
    $fixed = $content -replace '\[ct121-nfs-fix\.md\]\(\.\./\.\./\.\./doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix\.md\)', '[ct121-nfs-fix.md](./ct121-nfs-fix.md)'
    if ($fixed -ne $content -and $PSCmdlet.ShouldProcess($legacyImport, "Links anpassen")) {
        Set-Content -Path $legacyImport -Value $fixed -NoNewline
        Write-Host "  ~> $legacyImport (Links)" -ForegroundColor Cyan
    }
}

Write-Host ""
Write-Host "Fertig." -ForegroundColor Cyan

$DokuRepo = Join-Path $SrcRoot "..\doku" | Resolve-Path -ErrorAction SilentlyContinue
if ($DokuRepo) {
    Write-Host ""
    Write-Host "Naechster Schritt (doku-Repo, manuell):" -ForegroundColor Yellow
    Write-Host "  cd $($DokuRepo.Path)"
    Write-Host "  git status pve2/vm/121-paperless/"
    Write-Host "  git add pve2/vm/121-paperless/"
    Write-Host "  git commit -m ""sync: paperless-scripts from classifier"""
    Write-Host "  git push"
}
