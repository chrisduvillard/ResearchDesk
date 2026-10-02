# Host-side contract checks. Docker is stubbed; file copies and SHA-256 are real.
$ErrorActionPreference = 'Stop'
$backupScript = Join-Path $PSScriptRoot '../scripts/backup.ps1'
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($backupScript, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('backup-export-test-' + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $testRoot)
$source = Join-Path $testRoot 'source.tar.gz'
[IO.File]::WriteAllBytes($source, [Text.Encoding]::UTF8.GetBytes('known complete backup bytes'))
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
$global:backupExportTestState = @{ Mode = 'success'; Calls = 0; Source = $source; Hash = $sourceHash }

function docker {
    $global:backupExportTestState.Calls++
    $global:LASTEXITCODE = 0
    if (($args -join ' ') -eq 'compose exec -T collector python -m tracker.cli backup') {
        if ($global:backupExportTestState.Mode -eq 'backup_failure') { $global:LASTEXITCODE = 1; return }
        '/data/backups/2026-10-02.tar.gz'
    } elseif (($args[0..5] -join ' ') -eq 'compose exec -T collector python -c' -and
              $args.Count -eq 8 -and $args[7] -eq '/data/backups/2026-10-02.tar.gz') {
        if ($global:backupExportTestState.Mode -eq 'checksum_failure') { $global:LASTEXITCODE = 1; return }
        $global:backupExportTestState.Hash
    } elseif (($args[0..2] -join ' ') -eq 'compose cp collector:/data/backups/2026-10-02.tar.gz' -and $args.Count -eq 4) {
        Copy-Item -LiteralPath $global:backupExportTestState.Source -Destination $args[3]
        if ($global:backupExportTestState.Mode -eq 'copy_failure') { $global:LASTEXITCODE = 1; return }
        if ($global:backupExportTestState.Mode -eq 'corrupt_copy') { Add-Content -LiteralPath $args[3] -Value 'corrupt' }
    } else {
        throw "Unexpected Docker invocation: $args"
    }
}

try {
    $destination = Join-Path $testRoot 'external drive'
    [void](New-Item -ItemType Directory -Path $destination)
    $originalLocation = $PWD.Path
    1..2 | ForEach-Object {
        $output = & $backupScript -Destination $destination
        if ($output -notmatch 'Verified backup saved to:') { throw 'Successful export was not reported' }
    }
    if ($PWD.Path -ne $originalLocation) { throw 'Export changed the caller directory' }
    $archives = @(Get-ChildItem -LiteralPath $destination -Filter '*.tar.gz')
    if ($archives.Count -ne 2) { throw 'Repeated exports overwrote a backup' }
    foreach ($archive in $archives) {
        if ((Get-FileHash -LiteralPath $archive.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceHash) {
            throw 'Exported bytes differ from source'
        }
        $checksum = (Get-Content -LiteralPath ($archive.FullName + '.sha256') -Raw).Trim()
        if ($checksum -ne "$sourceHash  $($archive.Name)") { throw 'Checksum sidecar is incorrect' }
    }
    if (@(Get-ChildItem -LiteralPath $destination -Force).Count -ne 4) { throw 'Temporary copy was retained' }

    $failureMessages = @{
        backup_failure = 'The collector did not create a backup'
        checksum_failure = 'Could not read the backup checksum'
        copy_failure = 'Could not copy the backup'
        corrupt_copy = 'Backup copy failed its checksum check'
    }
    foreach ($failure in @('backup_failure', 'checksum_failure', 'copy_failure', 'corrupt_copy')) {
        $global:backupExportTestState.Mode = $failure
        $failedDestination = Join-Path $testRoot $failure
        [void](New-Item -ItemType Directory -Path $failedDestination)
        $caught = $null
        try { & $backupScript -Destination $failedDestination } catch { $caught = $_ }
        if ($null -eq $caught) { throw "$failure incorrectly succeeded" }
        if ($caught.Exception.Message -notlike "*$($failureMessages[$failure])*") { throw "$failure raised the wrong error: $caught" }
        if (@(Get-ChildItem -LiteralPath $failedDestination -Force).Count -ne 0) { throw "$failure left a backup behind" }
        if ($PWD.Path -ne $originalLocation) { throw "$failure changed the caller directory" }
    }

    $missing = Join-Path $testRoot 'unmounted drive'
    $callsBefore = $global:backupExportTestState.Calls
    $caught = $null
    try { & $backupScript -Destination $missing } catch { $caught = $_ }
    if ($null -eq $caught -or (Test-Path -LiteralPath $missing) -or $global:backupExportTestState.Calls -ne $callsBefore) {
        throw 'Missing destination must fail before running Docker or creating a folder'
    }
    Write-Output 'PowerShell backup export checks passed: repeated exports, checksums, four failures, and missing destination.'
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Remove-Variable -Name backupExportTestState -Scope Global
}
