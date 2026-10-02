"""Host export scripts must never advertise a failed or corrupted copy as a backup."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/backup.sh'


@pytest.fixture
def export_environment(tmp_path):
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    source = tmp_path / 'source.tar.gz'
    source.write_bytes(b'known complete backup bytes')
    docker = binaries / 'docker'
    docker.write_text('#!' + sys.executable + '\n' + '''import os,sys,shutil
args=sys.argv[1:]
if args == ['compose','exec','-T','collector','python','-m','tracker.cli','backup']:
    if os.environ.get('FAIL_BACKUP'): sys.exit(1)
    print('/data/backups/2026-10-02.tar.gz')
elif args[:6] == ['compose','exec','-T','collector','python','-c']:
    print(os.environ['SOURCE_HASH'])
elif args[:2] == ['compose','cp'] and args[2] == 'collector:/data/backups/2026-10-02.tar.gz':
    shutil.copyfile(os.environ['SOURCE_ARCHIVE'], args[3])
    if os.environ.get('CORRUPT_COPY'):
        with open(args[3], 'ab') as f: f.write(b'corrupt')
else:
    raise SystemExit('Unexpected Docker invocation: '+repr(args))
''')
    docker.chmod(0o755)
    return dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'],
                SOURCE_ARCHIVE=str(source), SOURCE_HASH=hashlib.sha256(source.read_bytes()).hexdigest(),
                TEST_DOCKER_SCRIPT=str(docker), TEST_PYTHON=sys.executable)


def run_export(destination, env, cwd):
    # Invoke the external-process double through Python so read-only Docker
    # tests also work when /tmp is mounted noexec.
    wrapper = 'docker() { "$TEST_PYTHON" "$TEST_DOCKER_SCRIPT" "$@"; }; export -f docker; bash "$1" "$2"'
    return subprocess.run(['bash', '-c', wrapper, '--', str(SCRIPT), str(destination)], env=env, cwd=cwd,
                          capture_output=True, text=True)


def test_export_checks_bytes_and_preserves_multiple_backups(tmp_path, export_environment):
    destination = tmp_path / 'external drive'
    destination.mkdir()
    for _ in range(2):
        result = run_export('external drive', export_environment, tmp_path)
        assert result.returncode == 0, result.stderr
    archives=list(destination.glob('*.tar.gz'))
    assert len(archives)==2
    for archive in archives:
        assert archive.read_bytes()==b'known complete backup bytes'
        assert Path(str(archive)+'.sha256').read_text().split()[0]==export_environment['SOURCE_HASH']


@pytest.mark.parametrize('failure', ['CORRUPT_COPY','FAIL_BACKUP'])
def test_failed_export_leaves_no_successful_backup(tmp_path, export_environment, failure):
    destination=tmp_path/'external'
    destination.mkdir()
    export_environment[failure]='1'
    result=run_export(destination,export_environment,tmp_path)
    assert result.returncode != 0
    assert not list(destination.iterdir())


def test_missing_destination_is_not_created(tmp_path, export_environment):
    destination=tmp_path/'unmounted-drive'
    result=run_export(destination,export_environment,tmp_path)
    assert result.returncode != 0
    assert not destination.exists()
