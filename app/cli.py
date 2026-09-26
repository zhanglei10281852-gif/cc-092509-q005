from __future__ import annotations

import argparse
import json
import sqlite3

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db, transaction


def command_init() -> None:
    init_db()
    print(json.dumps({"database": str(database_path()), "initialized": True}, ensure_ascii=False))


def command_check() -> None:
    init_db()
    connection = get_connection()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    print(json.dumps({"integrity": integrity, "foreign_keys": foreign_keys, "journal_mode": journal_mode}, ensure_ascii=False))
    if integrity != "ok" or foreign_keys != 1:
        raise SystemExit(1)


def command_smoke() -> None:
    from app.main import app

    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.status_code, "health": health.status_code, "service": root.json().get("service")}, ensure_ascii=False))
        if root.status_code != 200 or health.status_code != 200:
            raise SystemExit(1)


def command_deadlines_recover() -> None:
    """服务重启后补齐未完成节点的提醒排程（幂等）。"""
    from app.deadlines.service import DeadlineService

    init_db()
    connection = get_connection()
    with transaction(immediate=True) as connection:
        result = DeadlineService(connection).recover_reminders()
    print(json.dumps({"recovered": result}, ensure_ascii=False))


def command_deadlines_dispatch(worker: str) -> None:
    """领取并发送所有到期的期限提醒（可由 cron 反复调用）。"""
    from app.deadlines.service import DeadlineService

    init_db()
    connection = get_connection()
    with transaction(immediate=True) as connection:
        processed = DeadlineService(connection).dispatch_due_reminders(worker)
    print(json.dumps({"processed": processed, "worker": worker}, ensure_ascii=False))


def command_deadlines_sweep() -> None:
    """按辖区本地历法日把已过期的待处理节点标记为逾期。"""
    from app.deadlines.service import DeadlineService

    init_db()
    connection = get_connection()
    with transaction(immediate=True) as connection:
        count = DeadlineService(connection).sweep_overdue()
    print(json.dumps({"marked_overdue": count}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="知识产权档案服务维护命令")
    parser.add_argument(
        "command",
        choices=("init-db", "check-db", "smoke", "deadlines-recover", "deadlines-dispatch", "deadlines-sweep"),
    )
    parser.add_argument("--worker", default="cli-worker", help="deadlines 派发任务的执行者标识")
    args = parser.parse_args()
    if args.command == "deadlines-recover":
        command_deadlines_recover()
    elif args.command == "deadlines-dispatch":
        command_deadlines_dispatch(args.worker)
    else:
        {"init-db": command_init, "check-db": command_check, "smoke": command_smoke, "deadlines-sweep": command_deadlines_sweep}[args.command]()


if __name__ == "__main__":
    main()
