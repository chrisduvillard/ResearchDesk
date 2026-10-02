#!/usr/bin/env bash
# Export a verified backup to an existing external-drive or synced directory.
set -euo pipefail
if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
  echo 'Usage: bash scripts/backup.sh /path/to/existing-backup-folder' >&2
  echo 'Connect your backup drive first; the destination must already exist.' >&2
  exit 1
fi
destination=$(cd -- "$1" && pwd -P)
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if command -v sha256sum >/dev/null 2>&1; then
  hash_file() { sha256sum "$1" | cut -d ' ' -f 1; }
else
  hash_file() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
fi
temporary=$(mktemp "$destination/.research-desk-backup.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
archive=$(docker compose exec -T collector python -m tracker.cli backup)
if [[ ! "$archive" =~ ^/data/backups/[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.gz$ ]]; then
  echo 'The collector did not return a valid backup path.' >&2
  exit 1
fi
expected=$(docker compose exec -T collector python -c 'import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$archive")
docker compose cp "collector:$archive" "$temporary"
actual=$(hash_file "$temporary")
if [ "$expected" != "$actual" ]; then
  echo 'Backup copy failed its checksum check. Retry the export.' >&2
  exit 1
fi
name="research-desk-$(date -u +%Y%m%dT%H%M%SZ)-${temporary##*.}.tar.gz"
mv -- "$temporary" "$destination/$name"
printf '%s  %s\n' "$actual" "$name" > "$destination/$name.sha256"
printf 'Verified backup saved to: %s\n' "$destination/$name"
