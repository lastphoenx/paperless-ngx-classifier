# Sync paperless-ngx-classifier -> lokale 121-paperless Doku-Kopie (Windows).
#
# Source of truth: paperless-ngx-classifier (dieses Repo)
# Ziel: doku/pve2/vm/121-paperless/Doku/ (private Doku, kein Git)
#
# Nutzung:
#   .\scripts\sync-to-121-doku.ps1
#   .\scripts\sync-to-121-doku.ps1 -WhatIf
#
# Überschreibt NICHT: fix_*.py, create_tags.py, paperless-backup.sh (nur auf 121)

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
    "pre_consume.sh",
    "pre_consume_qr.py",
    "requirements-corr-manager.txt"
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot $_) (Join-Path $DstScripts $_)
}
Sync-File (Join-Path $SrcRoot "scripts\deploy-to-ct121.sh") (Join-Path $DstScripts "deploy-to-ct121.sh")

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
    @{ Src = "docs\Benutzerhandbuch_paper_manager.md"; Dst = "Benutzerhandbuch_paper_manager.md" }
) | ForEach-Object {
    Sync-File (Join-Path $SrcRoot $_.Src) (Join-Path $DstDocs $_.Dst)
}

Write-Host ""
Write-Host "Fertig." -ForegroundColor Cyan
