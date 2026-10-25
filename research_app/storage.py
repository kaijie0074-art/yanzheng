"""短连接、参数化SQL；研究修订和模型审计持久化，不保存凭据。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from .models import now


def uid() -> str:
    return str(uuid4())


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _doi_identity(value: str | None) -> str:
    value = unquote(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL UNIQUE, content TEXT NOT NULL,
 queries TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_runs (
 id TEXT PRIMARY KEY, study_revision INTEGER NOT NULL, query TEXT NOT NULL,
 requested_at TEXT NOT NULL, status TEXT NOT NULL, raw_json TEXT NOT NULL,
 screening_json TEXT NOT NULL, error_type TEXT
);
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL, year INTEGER,
 doi TEXT, official_url TEXT NOT NULL, access_level TEXT NOT NULL,
 bibliography_status TEXT NOT NULL DEFAULT 'unverified', sha256 TEXT UNIQUE,
 page_count INTEGER NOT NULL DEFAULT 0, inclusion_reason TEXT NOT NULL DEFAULT '',
 exclusion_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
 source_id TEXT NOT NULL REFERENCES sources(id), page_number INTEGER NOT NULL CHECK(page_number>0),
 text TEXT NOT NULL, PRIMARY KEY(source_id,page_number)
);
CREATE TABLE IF NOT EXISTS evidence_cards (
 id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, study_revision INTEGER NOT NULL,
 content TEXT NOT NULL, quote_match INTEGER NOT NULL, validation_note TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'unreviewed', review_note TEXT NOT NULL DEFAULT '',
 reviewed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_reviews (
 id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES evidence_cards(id),
 previous_content TEXT NOT NULL, previous_status TEXT NOT NULL,
 review_note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_runs (
 id TEXT PRIMARY KEY, study_revision INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY, study_revision INTEGER NOT NULL, chosen_candidate TEXT NOT NULL,
 reason TEXT NOT NULL, evidence_ids TEXT NOT NULL, experiment_run_id TEXT,
 status TEXT NOT NULL, created_at TEXT NOT NULL, invalidated_at TEXT,
 candidate_run_id TEXT REFERENCES candidate_runs(id)
);
CREATE TABLE IF NOT EXISTS model_runs (
 id TEXT PRIMARY KEY, study_revision INTEGER, task TEXT NOT NULL, backend TEXT NOT NULL,
 requested_model TEXT NOT NULL, actual_model TEXT, called_at TEXT NOT NULL,
 prompt_version TEXT NOT NULL, input_refs TEXT NOT NULL, status TEXT NOT NULL,
 output_text TEXT, error_type TEXT
);
"""


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.papers_dir = self.data_dir / "papers"
        self.papers_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "workbench.sqlite3"
        with self.connection() as db:
            db.executescript(SCHEMA)
            if "candidate_run_id" not in {
                row["name"] for row in db.execute("PRAGMA table_info(decisions)")
            }:
                db.execute(
                    "ALTER TABLE decisions ADD COLUMN candidate_run_id TEXT REFERENCES candidate_runs(id)"
                )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql: str, params=()) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def latest_brief(self) -> dict | None:
        rows = self.rows("SELECT * FROM briefs ORDER BY revision DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        row["content"] = json.loads(row["content"])
        row["queries"] = json.loads(row["queries"])
        return row

    def list_sources(self) -> list[dict]:
        rows = self.rows("SELECT * FROM sources ORDER BY created_at,id")
        for row in rows:
            row["authors"] = json.loads(row["authors"])
        return rows

    def get_source(self, source_id: str) -> dict:
        rows = [s for s in self.list_sources() if s["id"] == source_id]
        if not rows:
            raise ValueError("来源不存在")
        return rows[0]

    def get_pages(self, source_id: str) -> list[dict]:
        return self.rows(
            "SELECT * FROM pages WHERE source_id=? ORDER BY page_number", (source_id,)
        )

    def add_source(
        self,
        *,
        title: str,
        authors: list[str],
        year: int | None,
        official_url: str,
        doi: str | None = None,
        access_level: str = "metadata",
        inclusion_reason: str = "",
        exclusion_reason: str = "",
    ) -> str:
        if not title.strip() or not official_url.startswith(("https://", "http://")):
            raise ValueError("需填写标题与正式来源URL")
        if access_level not in {"metadata", "abstract", "fulltext"}:
            raise ValueError("访问层级无效")
        # fulltext只有成功导入PDF后才成立。
        if access_level == "fulltext":
            raise ValueError("请导入PDF以建立全文页码")
        source_id = uid()
        with self.connection() as db:
            db.execute(
                "INSERT INTO sources(id,title,authors,year,doi,official_url,access_level,inclusion_reason,exclusion_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    title.strip(),
                    encoded(authors),
                    year,
                    doi,
                    official_url,
                    access_level,
                    inclusion_reason,
                    exclusion_reason,
                    now(),
                ),
            )
        return source_id

    def update_source_metadata(
        self,
        source_id: str,
        *,
        title: str,
        authors: list[str],
        year: int | None,
        official_url: str,
        doi: str | None,
    ) -> None:
        if not title.strip() or not official_url.startswith(("https://", "http://")):
            raise ValueError("需填写标题与正式来源URL")
        updated = {
            "title": title,
            "authors": encoded(authors),
            "year": year,
            "official_url": official_url,
            "doi": doi,
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if source is None:
                raise ValueError("来源不存在")
            if any(source[key] != value for key, value in updated.items()):
                db.execute(
                    "UPDATE sources SET title=?,authors=?,year=?,official_url=?,doi=?,bibliography_status='unverified' WHERE id=?",
                    (title, encoded(authors), year, official_url, doi, source_id),
                )
                self._reset_source_reviews(db, source_id)
                detail = None
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_details'"
                ).fetchone():
                    detail = db.execute(
                        "SELECT metadata_json FROM paper_details WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                if detail:
                    identity_changed = (
                        _doi_identity(source["doi"]) != _doi_identity(doi)
                        or source["title"] != title
                        or source["official_url"] != official_url
                    )
                    if identity_changed:
                        # 新身份不能借用旧论文的摘要或开放全文下载位置。
                        db.execute(
                            "DELETE FROM paper_details WHERE source_id=?", (source_id,)
                        )
                        db.execute(
                            "UPDATE sources SET access_level='metadata' WHERE id=? AND sha256 IS NULL",
                            (source_id,),
                        )
                    else:
                        # 作者、年份校正不改变论文归属；同步显式书目快照。
                        metadata = json.loads(detail["metadata_json"])
                        metadata["source_identity"] = {
                            "title": title,
                            "authors": authors,
                            "year": year,
                            "doi": doi,
                            "official_url": official_url,
                        }
                        db.execute(
                            "UPDATE paper_details SET metadata_json=? WHERE source_id=?",
                            (encoded(metadata), source_id),
                        )

    def set_bibliography(self, source_id: str, confirmed: bool) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if source is None:
                raise ValueError("来源不存在")
            if confirmed and (
                not source["title"].strip()
                or not json.loads(source["authors"])
                or source["year"] is None
                or not source["official_url"].startswith(("https://", "http://"))
            ):
                raise ValueError("请先补全正式来源页面、标题、作者和年份再确认书目")
            db.execute(
                "UPDATE sources SET bibliography_status=? WHERE id=?",
                ("confirmed" if confirmed else "unverified", source_id),
            )
            if not confirmed:
                self._reset_source_reviews(db, source_id)

    def _reset_source_reviews(self, db, source_id: str) -> None:
        affected = []
        for card in db.execute("SELECT * FROM evidence_cards").fetchall():
            if json.loads(card["content"])["source_id"] == source_id:
                affected.append(card["id"])
                db.execute(
                    "INSERT INTO evidence_reviews VALUES(?,?,?,?,?,?)",
                    (
                        uid(),
                        card["id"],
                        card["content"],
                        card["review_status"],
                        encoded(
                            {
                                "action": "撤销或修改书目，复核失效",
                                "previous_review_note": card["review_note"],
                                "previous_reviewed_at": card["reviewed_at"],
                            }
                        ),
                        now(),
                    ),
                )
                db.execute(
                    "UPDATE evidence_cards SET review_status='unreviewed', review_note='', reviewed_at=NULL WHERE id=?",
                    (card["id"],),
                )
        self.invalidate_decisions(db, affected)

    @staticmethod
    def invalidate_decisions(db, card_ids: list[str]) -> None:
        invalid = set(card_ids)
        for row in db.execute(
            "SELECT id,evidence_ids FROM decisions WHERE status='final'"
        ).fetchall():
            if invalid.intersection(json.loads(row["evidence_ids"])):
                db.execute(
                    "UPDATE decisions SET status='draft',invalidated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
