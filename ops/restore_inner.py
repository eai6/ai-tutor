#!/usr/bin/env python
"""Restore a dump into this environment's database. Runs INSIDE the VPC.

Invoked as the command of a one-off ECS task by ``ops/restore_from_dump.sh``.
It is a file in the repo rather than a shell string smuggled through a
container override so that it can be read, reviewed and edited normally — the
override version needed character-code escaping to survive two levels of
quoting, which is not something anyone should have to debug at 2am.

Reads OPS_BUCKET and DUMP_KEY from the environment; DATABASE_URL arrives from
Secrets Manager like it does for every other task.

DESTRUCTIVE: drops and recreates the target database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlparse, urlunparse

DUMP_PATH = "/tmp/restore.dump"

# From the runbook's restore drill, 2026-07-30. Not assertions — the source
# database keeps growing — but a number wildly below these means the restore
# only half-happened, which otherwise looks like success.
EXPECTED = {
    "tutoring_sessionturn": 36109,
    "tutoring_tutorsession": 1106,
    "curriculum_lesson": 354,
    "auth_user": 389,
}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd[:3])} ...", flush=True)
    return subprocess.run(cmd, check=True, **kw)


def psql(dsn: str, sql: str, quiet: bool = False) -> str:
    out = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not quiet and out:
        print(out, flush=True)
    return out


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    bucket = os.environ["OPS_BUCKET"]
    key = os.environ["DUMP_KEY"]

    parsed = urlparse(database_url)
    dbname = parsed.path.lstrip("/")
    # You cannot drop the database you are connected to, so administrative
    # statements go to `postgres` instead. Rebuilt via urlunparse rather than
    # string surgery: the password is percent-encoded and naive replacement
    # corrupts it (see data.py's note on the same hazard).
    admin_dsn = urlunparse(parsed._replace(path="/postgres"))

    print(f"==> target database: {dbname}", flush=True)

    import boto3
    print(f"==> downloading s3://{bucket}/{key}", flush=True)
    boto3.client("s3").download_file(bucket, key, DUMP_PATH)
    print(f"    {os.path.getsize(DUMP_PATH) / 1_048_576:.1f} MB", flush=True)

    print(f"==> dropping and recreating {dbname}", flush=True)
    # WITH (FORCE) terminates any lingering backends. The service is scaled to
    # zero by the caller, but RDS itself may hold a session briefly after.
    psql(admin_dsn, f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE);')
    psql(admin_dsn, f'CREATE DATABASE "{dbname}";')

    # BEFORE pg_restore, not after. curriculum_curriculumchunk holds a 384-d
    # vector column with an HNSW index; without the extension present the
    # restore of that table fails, and it fails quietly enough that the app
    # looks healthy until a knowledge-base search silently returns nothing.
    print("==> creating the vector extension", flush=True)
    psql(database_url, "CREATE EXTENSION IF NOT EXISTS vector;")

    # --no-owner --no-acl: the RDS master user is rds_superuser, not a true
    # superuser, so restoring ownership of extension member objects raises
    # "must be owner of extension vector".
    print("==> pg_restore", flush=True)
    run(["pg_restore", "--no-owner", "--no-acl", "-j", "4",
         "-d", database_url, DUMP_PATH])

    print("==> verification", flush=True)
    extensions = psql(database_url, "select extname from pg_extension;", quiet=True)
    print(f"    extensions: {extensions.split()}", flush=True)
    if "vector" not in extensions:
        print("FAIL: the vector extension is missing after restore", file=sys.stderr)
        return 1

    problems = []
    for table, expected in EXPECTED.items():
        actual = int(psql(database_url, f"select count(*) from {table};", quiet=True))
        marker = "ok" if actual >= expected * 0.5 else "SUSPECT"
        print(f"    {table:<28} {actual:>7}  (drill: {expected})  {marker}", flush=True)
        if marker == "SUSPECT":
            problems.append(f"{table}={actual} vs ~{expected}")

    chunks = int(psql(database_url,
                      "select count(*) from curriculum_curriculumchunk;", quiet=True))
    print(f"    curriculum_curriculumchunk   {chunks:>7}  (pgvector)", flush=True)
    if chunks == 0:
        problems.append("curriculum_curriculumchunk is empty — pgvector data did not restore")

    if problems:
        print(f"FAIL: {'; '.join(problems)}", file=sys.stderr)
        return 1

    print("==> restore verified", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
