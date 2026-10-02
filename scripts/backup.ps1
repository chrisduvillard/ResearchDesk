# Export a verified backup to an existing external-drive or synced directory.
param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
    throw 'Connect your backup drive first; the destination folder must already exist.'
}
$folder = (Resolve-Path -LiteralPath $Destination).Path
$temporary = Join-Path $folder ('.research-desk-backup.' + [Guid]::NewGuid().ToString('N'))
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $archive = & docker compose exec -T collector python -m tracker.cli backup
    if ($LASTEXITCODE -ne 0 -or $archive -notmatch '^/data/backups/\d{4}-\d{2}-\d{2}\.tar\.gz$') {
        throw 'The collector did not create a backup. Check that the app is running.'
    }
    $expected = & docker compose exec -T collector python -c "import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest())" $archive
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the backup checksum.' }
    & docker compose cp "collector:$archive" $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy the backup to the destination.' }
    $actual = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw 'Backup copy failed its checksum check. Retry the export.' }
    $name = 'research-desk-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8) + '.tar.gz'
    $target = Join-Path $folder $name
    Move-Item -LiteralPath $temporary -Destination $target
    Set-Content -LiteralPath ($target + '.sha256') -Value "$actual  $name" -Encoding ascii
    Write-Output "Verified backup saved to: $target"
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    Pop-Location
}
