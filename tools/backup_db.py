"""Create verified SQLite snapshots and transfer them to a private S3 bucket."""

import argparse
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


def copy_checked(source: Path, target: Path):
    with (
        closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(target)) as copy,
    ):
        original.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("La copia no pasó integrity_check.")
        if copy.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("La copia contiene referencias inválidas.")
        if copy.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone() is None:
            raise ValueError("La copia no contiene migraciones del Grader.")
        copy.execute("PRAGMA journal_mode=DELETE").fetchone()


def transfer(source, target, *, upload=False):
    command = ["aws", "s3", "cp", str(source), str(target), "--only-show-errors"]
    if upload:
        command += ["--sse", "AES256"]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def backup(db: Path, prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uri = f"{prefix.rstrip('/')}/grader-{timestamp}-{uuid4().hex[:12]}.db"
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "snapshot.db"
        copy_checked(db, snapshot)
        transfer(snapshot, uri, upload=True)
    return uri


def restore(uri: str, target: Path):
    with TemporaryDirectory(dir=target.parent) as directory:
        downloaded = Path(directory) / "download.db"
        restored = Path(directory) / "restored.db"
        transfer(uri, downloaded)
        copy_checked(downloaded, restored)
        restored.chmod(0o600)
        # Atomically publish only if the destination does not already exist.
        os.link(restored, target)


def s3_uri(value: str) -> str:
    if not value.startswith("s3://") or not value[5:].strip("/"):
        raise argparse.ArgumentTypeError("Indica una URI privada s3://bucket/prefijo.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup")
    save.add_argument("--db", type=Path, default=Path("data/grader.db"))
    save.add_argument("--destination", required=True, type=s3_uri)
    recover = commands.add_parser("restore")
    recover.add_argument("--source", required=True, type=s3_uri)
    recover.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(backup(args.db, args.destination))
        else:
            restore(args.source, args.target)
    except (OSError, sqlite3.Error, subprocess.CalledProcessError, ValueError) as exc:
        parser.exit(1, f"No se completó {args.command} ({type(exc).__name__}). Revisa SQLite, el destino y los permisos AWS.\n")


if __name__ == "__main__":
    main()
