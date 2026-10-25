"""一次性接续旧研证数据；原库只读、备份到新笔记本，不带入模型/账号配置。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from api.competition_service import store_for, sync_source
from open_notebook.domain.notebook import Notebook


def copy_directory(source: Path, destination: Path):
    if source.is_symlink():
        raise ValueError("旧材料目录包含符号链接，未复制")
    if not source.is_dir():
        return
    for item in source.rglob("*"):
        if item.is_symlink() or not (item.is_dir() or item.is_file()):
            raise ValueError("旧材料存在链接或特殊文件，未复制")
    shutil.copytree(source, destination, dirs_exist_ok=True)


async def migrate(source_path: Path) -> dict:
    root = source_path.resolve(strict=True)
    database = root / "workbench.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("未找到可只读迁移的旧研究数据库")
    # A read-only snapshot is stable even when the old UI has an open SQLite connection.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT content FROM briefs ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("旧工作区没有已确认的问题，无需迁移")
        brief = json.loads(row[0])
    marker = (
        Path(".runtime")
        / f"legacy-{hashlib.sha256(str(root).encode()).hexdigest()}.json"
    )
    if marker.exists():
        result = json.loads(marker.read_text(encoding="utf-8"))
        await Notebook.get(result["notebook_id"])
        return {**result, "already_imported": True}
    notebook = Notebook(
        name=brief["question"][:150],
        description="接续已有真实研究；保留原证据人审状态、模型审计和实验，不代替本人核验",
    )
    await notebook.save()
    store = store_for(str(notebook.id))
    with (
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source,
        sqlite3.connect(store.db_path) as target,
    ):
        source.backup(target)
    # Initialize the additional native-source mapping after importing the core schema.
    store = store_for(str(notebook.id))
    for name in ("papers", "case", "experiments", "evaluations"):
        copy_directory(root / name, store.data_dir / name)
    audit = root / "sms_manifest.json"
    if audit.is_file() and not audit.is_symlink():
        shutil.copy2(audit, store.data_dir / audit.name)
    for source in store.list_sources():
        if source["sha256"]:
            await sync_source(store, str(notebook.id), source["id"])
    with store.connection() as db:
        counts = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT review_status,count(*) FROM evidence_cards GROUP BY review_status"
            )
        }
    result = {
        "notebook_id": str(notebook.id),
        "source_count": len(store.list_sources()),
        "review_counts": counts,
        "notice": "原工作区未改写；未复制模型配置或账号凭据；原待审核内容仍待审核。",
    }
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读接续旧研究到新产品；不调用AI")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(migrate(args.data_dir)), ensure_ascii=False, indent=2))
