"""Consistent, verified snapshots and staging of a clean SQLite database."""
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .archive import GROUP_CONSENT_MIGRATION
from .config import Settings, load_settings
from .storage import Storage


RELEASE_SCHEMA_VERSION = 1


def open_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def check_connection(conn):
    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise RuntimeError('SQLite integrity check failed')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('SQLite foreign key check failed')


def check(path):
    with closing(open_readonly(path)) as conn:
        check_connection(conn)


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).expanduser().absolute()
    if not source.is_file() or destination.exists() or destination.is_symlink():
        raise ValueError('Expected an existing source and an unused destination')
    fd, temporary = tempfile.mkstemp(prefix='.ayaka-', suffix='.sqlite3', dir=destination.parent)
    os.close(fd)
    tmp = Path(temporary)
    try:
        deadline = time.monotonic() + 45

        def limit(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('Snapshot deadline exceeded')

        with closing(open_readonly(source)) as live, closing(sqlite3.connect(tmp)) as candidate:
            live.backup(candidate, pages=128, progress=limit, sleep=0.1)
            check_connection(candidate)
            # Publish a self-contained file, independent of the source WAL.
            candidate.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if candidate.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise RuntimeError('Snapshot journal could not be finalized')
        with tmp.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(tmp, destination)
    finally:
        for path in (tmp, Path(str(tmp)+'-wal'), Path(str(tmp)+'-shm'), Path(str(tmp)+'-journal')):
            path.unlink(missing_ok=True)


def check_release_database(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise RuntimeError('Expected a separate release database file')
    with closing(open_readonly(path)) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        markers = {row[0] for row in conn.execute(
            "SELECT key FROM diary_meta WHERE key IN ('backfill', ?)", (GROUP_CONSENT_MIGRATION,))}
        if version != RELEASE_SCHEMA_VERSION or markers != {'backfill', GROUP_CONSENT_MIGRATION}:
            raise RuntimeError('Unexpected release database schema')


def load_release_settings() -> Settings:
    settings = load_settings()
    check_release_database(settings.db_path)
    return settings


async def prepare_release(settings: Settings, privacy_sources: tuple[Path, ...]) -> None:
    destination = settings.db_path
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Release destination must be unused')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ayaka-release-', dir=destination.parent) as directory:
        staged = Path(directory) / 'database.sqlite3'
        await Storage(replace(settings, db_path=staged)).initialize()
        with closing(sqlite3.connect(staged)) as conn:
            with conn:
                conn.execute('INSERT INTO diary_meta VALUES (?)', (GROUP_CONSENT_MIGRATION,))
                conn.execute(f'PRAGMA user_version={RELEASE_SCHEMA_VERSION}')
                for source in privacy_sources:
                    with closing(open_readonly(source)) as existing:
                        conn.executemany('INSERT OR IGNORE INTO diary_disabled VALUES (?)',
                                         existing.execute('SELECT chat_id FROM diary_disabled'))
                        conn.executemany('INSERT OR IGNORE INTO opt_outs VALUES (?, ?, ?)',
                                         existing.execute('SELECT chat_id, user_id, created_ts FROM opt_outs'))
            if conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise RuntimeError('Release journal could not be finalized')
            check_connection(conn)
        check_release_database(staged)
        staged.chmod(0o600)
        with staged.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(staged, destination)
