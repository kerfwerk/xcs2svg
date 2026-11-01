<#
.SYNOPSIS
  Koppelt "XCS zu SVG" einmalig mit LightBurn 2.2 und gibt den Schlüssel (Secret) aus.

.DESCRIPTION
  Auf dem Windows-PC ausführen, auf dem LightBurn läuft (LightBurn muss geöffnet sein).
  Sendet POST http://localhost:19520/api/connect mit den Berechtigungen "state" und "upload".
  LightBurn zeigt daraufhin einen Dialog, der innerhalb von 30 Sekunden bestätigt werden muss.
  Der Schlüssel wird angezeigt und in die Zwischenablage kopiert; danach in der Web-App unter
  "Einstellungen" einfügen und speichern. Nach Enter (spätestens nach 60 Sekunden) leert das Skript
  die Zwischenablage wieder, sofern sie noch den Schlüssel enthält. Das Skript speichert selbst nichts.

  Aufruf (Windows PowerShell 5.1 oder PowerShell 7):
    powershell -ExecutionPolicy Bypass -File tools\lightburn-pair.ps1

.PARAMETER Port
  API-Port von LightBurn (fest 19520; MillMage: 19521).

.PARAMETER NoClipboard
  Schlüssel nicht in die Zwischenablage kopieren (nur anzeigen).

.PARAMETER ClipboardSeconds
  So lange bleibt der Schlüssel höchstens in der Zwischenablage (Standard 60 Sekunden).
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 19520,
    [switch]$NoClipboard,
    [ValidateRange(5, 600)]
    [int]$ClipboardSeconds = 60
)

$ErrorActionPreference = 'Stop'
$uri = "http://localhost:$Port/api/connect"
$body = @{ application_name = 'XCS zu SVG'; capabilities = @('state', 'upload') } | ConvertTo-Json -Compress

Write-Host "Kopplung mit LightBurn unter $uri ..."
Write-Host 'Bitte jetzt in LightBurn den Dialog "XCS zu SVG" bestätigen (innerhalb von 30 Sekunden).' -ForegroundColor Yellow

try {
    $response = Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $body -TimeoutSec 40
}
catch {
    $status = $null
    $webResponse = $_.Exception.Response
    if ($null -ne $webResponse) {
        try { $status = [int]$webResponse.StatusCode } catch { $status = $null }
    }
    if ($status -eq 403) {
        Write-Host 'Die Kopplung wurde in LightBurn abgelehnt oder nicht innerhalb von 30 Sekunden bestätigt (HTTP 403).' -ForegroundColor Red
        Write-Host 'Skript erneut starten und den Dialog in LightBurn bestätigen.'
        exit 3
    }
    if ($status -eq 400) {
        Write-Host 'LightBurn hat die Anfrage abgelehnt (HTTP 400). Ist LightBurn mindestens Version 2.2?' -ForegroundColor Red
        exit 4
    }
    if ($null -ne $status) {
        Write-Host "LightBurn hat unerwartet mit HTTP $status geantwortet." -ForegroundColor Red
        exit 5
    }
    Write-Host "LightBurn ist unter localhost:$Port nicht erreichbar (Verbindung abgelehnt oder keine Antwort)." -ForegroundColor Red
    Write-Host 'Läuft LightBurn 2.2 oder neuer auf diesem PC? Die Kopplung funktioniert nur direkt auf dem PC mit LightBurn.'
    exit 2
}

$secret = $null
if ($null -ne $response -and $response.PSObject.Properties.Name -contains 'secret') {
    $secret = [string]$response.secret
}
if ([string]::IsNullOrWhiteSpace($secret)) {
    Write-Host 'LightBurn hat zugestimmt, aber keinen Schlüssel zurückgegeben.' -ForegroundColor Red
    exit 6
}

Write-Host ''
Write-Host 'Gekoppelt. Schlüssel (wie ein Passwort behandeln):' -ForegroundColor Green
Write-Host $secret
$copied = $false
if (-not $NoClipboard) {
    try {
        Set-Clipboard -Value $secret
        $copied = $true
        Write-Host 'Der Schlüssel liegt in der Zwischenablage.'
    }
    catch {
        Write-Host 'Kopieren in die Zwischenablage fehlgeschlagen – bitte den Schlüssel oben markieren und kopieren.'
    }
}
Write-Host ''
Write-Host 'Jetzt in der Web-App (http://localhost:8080/einstellungen) einfügen und speichern.'
Write-Host 'Für den Zugriff aus dem Container zusätzlich in LightBurn unter Einstellungen > Erweiterungen'
Write-Host '"Allow API Access From Network" einschalten.'

if ($copied) {
    # The secret is permanent: do not leave it in the clipboard for other programs or later pastes.
    Write-Host ''
    Write-Host "Nach dem Einfügen Enter drücken – die Zwischenablage wird dann geleert (spätestens nach $ClipboardSeconds Sekunden)." -ForegroundColor Yellow
    $deadline = (Get-Date).AddSeconds($ClipboardSeconds)
    try {
        while ((Get-Date) -lt $deadline) {
            if ([Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq 'Enter') { break }
            Start-Sleep -Milliseconds 200
        }
    }
    catch {
        # No interactive console (input redirected): just wait out the time.
        $left = ($deadline - (Get-Date)).TotalSeconds
        if ($left -gt 0) { Start-Sleep -Seconds ([int][Math]::Ceiling($left)) }
    }
    try {
        if ((Get-Clipboard -Raw) -eq $secret) {
            Set-Clipboard -Value $null
            Write-Host 'Zwischenablage geleert.'
        }
        else {
            Write-Host 'Die Zwischenablage enthält inzwischen etwas anderes und wurde nicht verändert.'
        }
    }
    catch {
        Write-Host 'Die Zwischenablage ließ sich nicht leeren – bitte selbst etwas anderes kopieren.' -ForegroundColor Red
    }
    Write-Host 'Ist der Zwischenablage-Verlauf eingeschaltet (Windows-Taste + V), den Schlüssel dort zusätzlich löschen.'
}
