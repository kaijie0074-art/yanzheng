"""固定 SMS 实验；正文仅留在本地内存，持久记录仅含行标识与指标。"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import platform
import random
import re
import statistics
import time
import unicodedata
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterator

from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline

OFFICIAL_COUNT = 5574
SOURCE_URL = "https://archive.ics.uci.edu/dataset/228/sms+spam+collection"
CANDIDATES = ("word_nb", "word_lr")
BUDGETS = (200, 500, 1000)
SEEDS = (0, 1, 2, 3, 4)
METRICS = (
    "spam_f1",
    "spam_precision",
    "spam_recall",
    "macro_f1",
    "ham_false_positive_rate",
)
TIMINGS = ("training_seconds", "evaluation_seconds", "total_seconds")
CSV_FIELDS = (
    "candidate",
    "budget",
    "seed",
    *METRICS,
    "spam_support",
    "ham_support",
    *TIMINGS,
)
BASE_FILES = (
    "manifest.json",
    "config.json",
    "split_ids.json",
    "validation.csv",
    "summary.json",
    "run.log",
)
OPTIONAL_FILES = ("frozen_selection.json", "test.csv", "test_attempt.json")


class ExperimentError(ValueError):
    """可安全显示的实验错误，禁止包含短信或底层解析器原始错误。"""


@dataclass(frozen=True)
class _Record:
    row_id: str
    line_number: int
    label: str
    text: str
    key_hash: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ExperimentError("实验记录缺失或损坏，不能展示或确认结果。") from None


def _key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _snapshot(data_dir: str | Path) -> tuple[dict, list[_Record]]:
    root = Path(data_dir)
    try:
        raw = (root / "case" / "SMSSpamCollection").read_bytes()
    except OSError:
        raise ExperimentError("缺少官方 SMS 原始文件，请先准备固定公开材料。") from None
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "audited_at": _now(),
        "source_url": SOURCE_URL,
        "license": "CC BY 4.0",
        "official_sample_count": OFFICIAL_COUNT,
        "snapshot_sha256": _sha(raw),
        "raw_bytes": len(raw),
        "status": "failed",
        "total_lines": 0,
        "actual_parsed_count": 0,
        "label_counts": {"ham": 0, "spam": 0},
        "empty_lines": [],
        "bad_lines": [],
        "readme_check": {},
    }
    readme_path = root / "case" / "readme"
    if readme_path.is_file():
        readme = readme_path.read_bytes()
        encoding = "utf-8"
        try:
            readme_text = readme.decode(encoding, errors="strict")
        except UnicodeError:
            encoding = "iso-8859-1"
            readme_text = readme.decode(encoding, errors="strict")
        # readme 自带示例短信，不输出或保存示例，仅提取计数声明。
        description = "\n".join(
            line
            for line in readme_text.splitlines()
            if not re.match(r"^\s*(?:ham|spam)\s", line, re.IGNORECASE)
        )
        counts = sorted(
            {
                int(n.replace(",", ""))
                for n in re.findall(
                    r"\b(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,6})\b", description
                )
            }
        )
        declared = {}
        patterns = {
            "total": r"([0-9][0-9,]*)\s+messages,\s+tagged",
            "ham": r"a total of\s+([0-9][0-9,]*)\s+SMS legitimate messages",
            "spam": r"a total of\s+([0-9][0-9,]*)\s+\([^)]*\)\s+spam messages",
        }
        for name, pattern in patterns.items():
            match = re.search(pattern, description, re.IGNORECASE)
            declared[name] = int(match[1].replace(",", "")) if match else None
        manifest["readme_check"] = {
            "status": "checked",
            "sha256": _sha(readme),
            "decoding": encoding,
            "encoding_note": "UTF-8 严格解码失败时采用 ISO-8859-1 无损字节映射；不是原始编码检测结论。",
            "official_count_mentioned": OFFICIAL_COUNT in counts,
            "numeric_mentions": counts,
            "declared_counts": declared,
            "note": "已读取官方随包 readme 并核对数字；保留实际解析数，不补行或替换第三方数据。",
        }
    else:
        manifest["readme_check"] = {"status": "missing"}
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        manifest["error_code"] = "INVALID_UTF8"
        _save(root / "sms_manifest.json", manifest)
        raise ExperimentError(
            "SMS 数据不是严格 UTF-8；已停止，审计不包含原文。"
        ) from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    manifest["total_lines"] = len(lines)
    groups: dict[str, list[_Record]] = {}
    for number, line in enumerate(lines, 1):
        line = line.removesuffix("\r")
        if not line.strip():
            manifest["empty_lines"].append(number)
            continue
        label, separator, body = line.partition("\t")
        if not separator or label not in ("ham", "spam") or not body.strip():
            manifest["bad_lines"].append(number)
            continue
        normalized = _key(body)
        if not normalized:
            manifest["bad_lines"].append(number)
            continue
        manifest["actual_parsed_count"] += 1
        manifest["label_counts"][label] += 1
        record = _Record(
            _sha(f"{manifest['snapshot_sha256']}:{number}".encode()),
            number,
            label,
            body,
            _sha(normalized.encode("utf-8")),
        )
        groups.setdefault(normalized, []).append(record)
    manifest["difference_from_official"] = (
        manifest["actual_parsed_count"] - OFFICIAL_COUNT
    )
    manifest["readme_check"]["actual_count_mentioned"] = manifest[
        "actual_parsed_count"
    ] in manifest["readme_check"].get("numeric_mentions", [])
    declared_counts = manifest["readme_check"].get("declared_counts", {})
    manifest["readme_check"]["actual_minus_declared"] = {
        label: (
            manifest["actual_parsed_count"]
            if label == "total"
            else manifest["label_counts"][label]
        )
        - count
        for label, count in declared_counts.items()
        if count is not None
    }
    manifest["count_note"] = (
        "官方元数据与文件实际计数分别保留；差异不自动判定文件有误，也不补行。"
    )
    if manifest["empty_lines"] or manifest["bad_lines"] or not groups:
        manifest["error_code"] = "INVALID_ROWS"
        _save(root / "sms_manifest.json", manifest)
        raise ExperimentError(
            "SMS 存在空行、非法标签、缺少 TAB 或空正文；已停止，请查看审计中的行号。"
        )
    if manifest["readme_check"]["status"] != "checked":
        manifest["error_code"] = "README_UNAVAILABLE"
        _save(root / "sms_manifest.json", manifest)
        raise ExperimentError("官方 readme 缺失，尚不能完成数据审计。")
    retained: list[_Record] = []
    conflicts: list[dict] = []
    duplicates: list[dict] = []
    for items in groups.values():
        if len({r.label for r in items}) > 1:
            conflicts.append(
                {
                    "key_sha256": items[0].key_hash,
                    "row_ids": [r.row_id for r in items],
                    "line_numbers": [r.line_number for r in items],
                }
            )
        else:
            retained.append(items[0])
            if len(items) > 1:
                duplicates.append(
                    {
                        "kept_row_id": items[0].row_id,
                        "removed_row_ids": [r.row_id for r in items[1:]],
                        "key_sha256": items[0].key_hash,
                    }
                )
    retained.sort(key=lambda r: r.line_number)
    manifest.update(
        {
            "status": "success",
            "deduplication": "NFKC + casefold + whitespace collapse; same-label first; conflicting-label entire group excluded",
            "retained_count": len(retained),
            "retained_label_counts": {
                label: sum(r.label == label for r in retained)
                for label in ("ham", "spam")
            },
            "duplicate_removed_count": sum(
                len(g["removed_row_ids"]) for g in duplicates
            ),
            "conflict_group_count": len(conflicts),
            "conflict_row_count": sum(len(g["row_ids"]) for g in conflicts),
            "duplicate_groups": duplicates,
            "conflict_groups": conflicts,
            "rows": [
                {
                    "row_id": r.row_id,
                    "original_line_number": r.line_number,
                    "label": r.label,
                    "dedup_key_sha256": r.key_hash,
                }
                for r in retained
            ],
            "limitation": "规范化去重不保证消除所有模板改写泄漏；文件顺序不代表时间。",
        }
    )
    _save(root / "sms_manifest.json", manifest)
    return manifest, retained


def audit(data_dir: str | Path = "data") -> dict:
    """严格解析并保存不含正文的数据审计。失败时也保存安全的审计元数据。"""
    return _snapshot(data_dir)[0]


def _config() -> dict:
    return {
        "schema_version": 1,
        "dependencies": {
            name: version(name) for name in ("scikit-learn", "numpy", "scipy")
        },
        "python": platform.python_version(),
        "split": {
            "seed": 42,
            "test_fraction": 0.2,
            "validation_fraction_of_remaining": 0.25,
            "stratified": True,
        },
        "deduplication": "NFKC_casefold_whitespace_first_conflicts_excluded",
        "budgets": list(BUDGETS),
        "sampling_seeds": list(SEEDS),
        "candidate_order": list(CANDIDATES),
        "vectorizer": {
            "analyzer": "word",
            "ngram_range": [1, 2],
            "lowercase": True,
            "min_df": 1,
            "max_features": 50000,
            "norm": "l2",
            "stop_words": None,
        },
        "classifiers": {
            "word_nb": {"alpha": 1.0},
            "word_lr": {
                "C": 1.0,
                "solver": "liblinear",
                "max_iter": 2000,
                "random_state": 42,
            },
        },
        "selection": {
            "budget": 1000,
            "primary": "spam_f1",
            "secondary": "ham_false_positive_rate",
            "tolerance": 1e-12,
            "tie_order": list(CANDIDATES),
        },
        "positive_label": "spam",
        "std_ddof": 1,
    }


def _assert_disjoint(parts: dict[str, list[str]], records: list[_Record]) -> None:
    by_id = {r.row_id: r for r in records}
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for name in ("train_pool", "validation", "test"):
        ids = parts[name]
        if len(ids) != len(set(ids)) or any(row_id not in by_id for row_id in ids):
            raise ExperimentError("划分包含重复或未知行标识。")
        keys = {by_id[row_id].key_hash for row_id in ids}
        if seen_ids.intersection(ids) or seen_keys.intersection(keys):
            raise ExperimentError("检测到跨集合的行或规范化正文泄漏，实验停止。")
        seen_ids.update(ids)
        seen_keys.update(keys)
    if seen_ids != set(by_id):
        raise ExperimentError("划分没有恰好覆盖去重后的全部样本。")


def _sample_ids(train: list[_Record], budget: int, seed: int) -> list[str]:
    if budget > len(train) or budget < 2:
        raise ExperimentError("去重后训练池不足以满足固定标签预算。")
    by_label = {
        label: [r.row_id for r in train if r.label == label]
        for label in ("ham", "spam")
    }
    spam_count = int(math.floor(budget * len(by_label["spam"]) / len(train) + 0.5))
    counts = {"spam": spam_count, "ham": budget - spam_count}
    if any(
        counts[label] < 1 or counts[label] > len(by_label[label]) for label in by_label
    ):
        raise ExperimentError("固定标签预算无法包含两个类别。")
    rng = random.Random(seed)
    for ids in by_label.values():
        rng.shuffle(ids)
    return by_label["ham"][: counts["ham"]] + by_label["spam"][: counts["spam"]]


def _split(records: list[_Record]) -> dict:
    ids = [r.row_id for r in records]
    labels = {r.row_id: r.label for r in records}
    try:
        remaining, test = train_test_split(
            ids, test_size=0.2, random_state=42, stratify=[labels[i] for i in ids]
        )
        train, validation = train_test_split(
            remaining,
            test_size=0.25,
            random_state=42,
            stratify=[labels[i] for i in remaining],
        )
    except ValueError:
        raise ExperimentError(
            "样本量或类别分布不足以进行固定分层 60/20/20 划分。"
        ) from None
    parts = {"train_pool": train, "validation": validation, "test": test}
    _assert_disjoint(parts, records)
    lookup = {r.row_id: r for r in records}
    train_records = [lookup[i] for i in train]
    parts["samples"] = {
        str(budget): {
            str(seed): _sample_ids(train_records, budget, seed) for seed in SEEDS
        }
        for budget in BUDGETS
    }
    return parts


def _pipeline(candidate: str) -> Pipeline:
    vectorizer = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        lowercase=True,
        min_df=1,
        max_features=50000,
        norm="l2",
        stop_words=None,
    )
    if candidate == "word_nb":
        classifier = MultinomialNB(alpha=1.0)
    elif candidate == "word_lr":
        classifier = LogisticRegression(
            C=1.0, solver="liblinear", max_iter=2000, random_state=42
        )
    else:
        raise ExperimentError("不是固定候选管道。")
    return Pipeline([("tfidf", vectorizer), ("classifier", classifier)])


def _metrics(truth: list[str], predicted: list[str]) -> dict:
    if (
        not truth
        or len(truth) != len(predicted)
        or set(truth) != {"ham", "spam"}
        or not set(predicted).issubset({"ham", "spam"})
    ):
        raise ExperimentError("评估必须包含两个真实类别且预测长度一致。")
    ham_count = sum(label == "ham" for label in truth)
    fp = sum(
        actual == "ham" and guess == "spam" for actual, guess in zip(truth, predicted)
    )
    return {
        "spam_f1": float(f1_score(truth, predicted, pos_label="spam", zero_division=0)),
        "spam_precision": float(
            precision_score(truth, predicted, pos_label="spam", zero_division=0)
        ),
        "spam_recall": float(
            recall_score(truth, predicted, pos_label="spam", zero_division=0)
        ),
        "macro_f1": float(
            f1_score(
                truth,
                predicted,
                average="macro",
                labels=["ham", "spam"],
                zero_division=0,
            )
        ),
        "ham_false_positive_rate": fp / ham_count,
        "spam_support": len(truth) - ham_count,
        "ham_support": ham_count,
    }


def _fit_evaluate(
    candidate: str, train: list[_Record], evaluation: list[_Record]
) -> dict:
    model = _pipeline(candidate)
    start = time.perf_counter()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            model.fit([r.text for r in train], [r.label for r in train])
        fitted = time.perf_counter()
        predicted = model.predict([r.text for r in evaluation]).tolist()
        metrics = _metrics([r.label for r in evaluation], predicted)
        ended = time.perf_counter()
    except ConvergenceWarning:
        raise ExperimentError("分类器未收敛，实验失败；不返回部分指标。") from None
    except (ValueError, ArithmeticError):
        raise ExperimentError(
            "固定管道拟合或评估失败（可能为空词表），未生成有效结果。"
        ) from None
    return {
        **metrics,
        "training_seconds": fitted - start,
        "evaluation_seconds": ended - fitted,
        "total_seconds": ended - start,
    }


def _stats(values: list[float]) -> dict:
    return {
        "mean": statistics.mean(values),
        "sample_std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def _aggregate(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["candidate"], row["budget"]), []).append(row)
    return [
        {
            "candidate": candidate,
            "budget": budget,
            "metrics": {
                metric: _stats([r[metric] for r in items]) for metric in METRICS
            },
            "timing": {
                metric: _stats([r[metric] for r in items]) for metric in TIMINGS
            },
            "spam_support": items[0]["spam_support"],
            "ham_support": items[0]["ham_support"],
            "seeds": [r["seed"] for r in items],
        }
        for (candidate, budget), items in groups.items()
    ]


def _paired(rows: list[dict]) -> list[dict]:
    lookup = {(r["candidate"], r["budget"], r["seed"]): r for r in rows}
    return [
        {
            "budget": budget,
            "direction": "word_lr_minus_word_nb",
            "metrics": {
                metric: {
                    **_stats(
                        [
                            lookup[("word_lr", budget, seed)][metric]
                            - lookup[("word_nb", budget, seed)][metric]
                            for seed in SEEDS
                        ]
                    ),
                    "by_seed": {
                        str(seed): lookup[("word_lr", budget, seed)][metric]
                        - lookup[("word_nb", budget, seed)][metric]
                        for seed in SEEDS
                    },
                }
                for metric in METRICS
            },
        }
        for budget in BUDGETS
    ]


def _recommend(aggregates: list[dict]) -> str:
    final = {a["candidate"]: a for a in aggregates if a["budget"] == 1000}
    if set(final) != set(CANDIDATES):
        raise ExperimentError("缺少 1000 标签预算的两个候选验证结果。")
    nb, lr = (final[c]["metrics"] for c in CANDIDATES)
    difference = lr["spam_f1"]["mean"] - nb["spam_f1"]["mean"]
    if abs(difference) >= 1e-12:
        return "word_lr" if difference > 0 else "word_nb"
    difference = (
        lr["ham_false_positive_rate"]["mean"] - nb["ham_false_positive_rate"]["mean"]
    )
    if difference != 0:
        return "word_lr" if difference < 0 else "word_nb"
    return "word_nb"


def _csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _log(path: Path, event: str) -> None:
    with (path / "run.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{_now()} {event}\n")


def _run_path(run_id: str, data_dir: str | Path) -> Path:
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ExperimentError("实验标识必须是规范 UUID。") from None
    return Path(data_dir) / "experiments" / run_id


@contextmanager
def _run_lock(path: Path) -> Iterator[None]:
    """冻结与测试共用跨进程锁；锁文件不封存、不删除，避免更换 inode。"""
    try:
        handle = (path / ".transaction.lock").open("a+b")
    except OSError:
        raise ExperimentError("实验目录缺失或无法访问，不能冻结或确认结果。") from None
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExperimentError(
                "本次运行处理中，请待当前冻结或保留集评估结束后再操作。"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _seal(path: Path) -> None:
    names = list(BASE_FILES)
    for optional in OPTIONAL_FILES:
        if (path / optional).is_file():
            names.append(optional)
    _save(
        path / "integrity.json",
        {name: _sha((path / name).read_bytes()) for name in names},
    )


def compare(data_dir: str | Path = "data") -> dict:
    """仅评估验证集；不自动冻结，更不读取测试集进行预测。"""
    started = time.perf_counter()
    manifest, records = _snapshot(data_dir)
    config = _config()
    split = _split(records)
    run_id = str(uuid.uuid4())
    path = _run_path(run_id, data_dir)
    path.mkdir(parents=True, exist_ok=False)
    _save(path / "manifest.json", manifest)
    _save(path / "config.json", config)
    _save(path / "split_ids.json", split)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": _now(),
        "status": "running",
        "snapshot_sha256": manifest["snapshot_sha256"],
        "config_sha256": _sha(_json_bytes(config)),
        "split_sha256": _sha(_json_bytes(split)),
        "recommended_candidate": None,
        "validation_aggregates": [],
        "paired_differences": [],
        "test_aggregates": [],
        "limitations": [
            "五个抽样种子共享同一验证/测试集，样本标准差不是独立测试集置信区间。",
            "结论仅限历史英文 SMS 数据与当前固定配置，不外推至中文反诈或生产系统。",
            "标签预算仅计训练样本，不代表真实人工标注成本；验证与测试标签不计入预算。",
        ],
    }
    _save(path / "summary.json", summary)
    _log(path, "validation_started")
    by_id = {r.row_id: r for r in records}
    evaluation = [by_id[i] for i in split["validation"]]
    rows: list[dict] = []
    try:
        for budget in BUDGETS:
            for seed in SEEDS:
                train = [by_id[i] for i in split["samples"][str(budget)][str(seed)]]
                for candidate in CANDIDATES:
                    result = _fit_evaluate(candidate, train, evaluation)
                    rows.append(
                        {
                            "candidate": candidate,
                            "budget": budget,
                            "seed": seed,
                            **result,
                        }
                    )
                    _log(
                        path,
                        f"validation_fit candidate={candidate} budget={budget} seed={seed} seconds={result['total_seconds']:.6f}",
                    )
        aggregates = _aggregate(rows)
        summary.update(
            {
                "status": "validation_complete",
                "validation_aggregates": aggregates,
                "paired_differences": _paired(rows),
                "recommended_candidate": _recommend(aggregates),
                "validation_fit_count": len(rows),
                "validation_completed_at": _now(),
                "actual_timing": {
                    "validation_wall_seconds": time.perf_counter() - started,
                    "validation_fit_seconds": sum(r["total_seconds"] for r in rows),
                },
            }
        )
        _csv(path / "validation.csv", rows)
        _save(path / "summary.json", summary)
        _log(path, "validation_complete; awaiting_explicit_human_freeze")
        _seal(path)
    except Exception as exc:
        summary.update(
            {"status": "failed", "error_code": type(exc).__name__, "failed_at": _now()}
        )
        _save(path / "summary.json", summary)
        _log(path, f"validation_failed error_type={type(exc).__name__}")
        if isinstance(exc, ExperimentError):
            raise
        raise ExperimentError(
            "验证实验失败，已保留安全日志；没有有效指标可供选择。"
        ) from None
    return summary


def verified_summary(run_id: str, data_dir: str | Path = "data") -> dict:
    """验证快照、运行参数与产物哈希；不执行模型，也不返回原始短信。"""
    path = _run_path(run_id, data_dir)
    integrity = _read_json(path / "integrity.json")
    if (
        not isinstance(integrity, dict)
        or not set(BASE_FILES).issubset(integrity)
        or set(integrity) - {*BASE_FILES, *OPTIONAL_FILES}
    ):
        raise ExperimentError("实验完整性清单不合法。")
    for name in (*BASE_FILES, *OPTIONAL_FILES):
        file = path / name
        if file.exists() != (name in integrity):
            raise ExperimentError("实验产物与完整性清单不一致。")
        if name in integrity:
            try:
                digest = _sha(file.read_bytes())
            except OSError:
                raise ExperimentError("实验产物无法读取。") from None
            if digest != integrity[name]:
                raise ExperimentError("实验产物哈希不一致，拒绝使用被改动结果。")
    summary = _read_json(path / "summary.json")
    config = _read_json(path / "config.json")
    manifest = _read_json(path / "manifest.json")
    split = _read_json(path / "split_ids.json")
    if not all(isinstance(value, dict) for value in (summary, config, manifest, split)):
        raise ExperimentError("实验记录结构不合法。")
    if (
        summary.get("status") not in ("validation_complete", "confirmed")
        or summary.get("run_id") != run_id
    ):
        raise ExperimentError("实验尚未成功，不能引用其结果。")
    try:
        snapshot = _sha((Path(data_dir) / "case" / "SMSSpamCollection").read_bytes())
    except OSError:
        raise ExperimentError("当前数据快照缺失，不能核验历史结果。") from None
    if snapshot != summary.get("snapshot_sha256") or snapshot != manifest.get(
        "snapshot_sha256"
    ):
        raise ExperimentError("当前原始数据与实验快照哈希不一致。")
    config_sha = _sha(_json_bytes(config))
    if config_sha != summary.get("config_sha256") or config_sha != _sha(
        _json_bytes(_config())
    ):
        raise ExperimentError("参数或依赖版本与实验配置哈希不一致。")
    if summary.get("split_sha256") != _sha(_json_bytes(split)):
        raise ExperimentError("数据划分哈希不一致。")
    if summary.get("recommended_candidate") != _recommend(
        summary.get("validation_aggregates", [])
    ):
        raise ExperimentError("推荐候选与固定验证择优规则不一致。")
    if "frozen_selection.json" in integrity:
        frozen = _read_json(path / "frozen_selection.json")
        if (
            not isinstance(frozen, dict)
            or frozen.get("run_id") != run_id
            or frozen.get("chosen_candidate") != summary["recommended_candidate"]
            or frozen.get("snapshot_sha256") != snapshot
            or frozen.get("config_sha256") != config_sha
            or frozen.get("split_sha256") != summary["split_sha256"]
            or not isinstance(frozen.get("reason"), str)
            or not frozen["reason"].strip()
            or not isinstance(frozen.get("study_revision"), int)
            or isinstance(frozen["study_revision"], bool)
            or frozen["study_revision"] < 1
            or not frozen.get("confirmed_at")
        ):
            raise ExperimentError("人工冻结记录不合法或与运行不一致。")
        summary["frozen_selection"] = frozen
    if (summary["status"] == "confirmed") != ("test.csv" in integrity):
        raise ExperimentError("保留集完成状态与指标文件不一致。")
    if summary["status"] == "confirmed" and "frozen_selection" not in summary:
        raise ExperimentError("保留集结果缺少人工冻结记录。")
    return summary


def freeze_selection(
    run_id: str, *, reason: str, study_revision: int, data_dir: str | Path = "data"
) -> dict:
    """只能由人工按钮调用；冻结验证推荐，不能接受用户或模型指定其他赢家。"""
    if not isinstance(reason, str) or not reason.strip():
        raise ExperimentError("人工确认必须填写非空理由。")
    if (
        not isinstance(study_revision, int)
        or isinstance(study_revision, bool)
        or study_revision < 1
    ):
        raise ExperimentError("人工确认必须绑定有效研究问题版本。")
    path = _run_path(run_id, data_dir)
    with _run_lock(path):
        summary = verified_summary(run_id, data_dir)
        if "frozen_selection" in summary:
            existing = summary["frozen_selection"]
            if (
                existing["reason"] == reason.strip()
                and existing["study_revision"] == study_revision
            ):
                return existing
            raise ExperimentError("本次运行已经冻结，不能变更赢家、理由或研究版本。")
        selection = {
            "run_id": run_id,
            "chosen_candidate": summary["recommended_candidate"],
            "reason": reason.strip(),
            "study_revision": study_revision,
            "snapshot_sha256": summary["snapshot_sha256"],
            "config_sha256": summary["config_sha256"],
            "split_sha256": summary["split_sha256"],
            "confirmed_at": _now(),
        }
        _save(path / "frozen_selection.json", selection)
        _log(path, "human_selection_frozen")
        _seal(path)
        return selection


def confirm(data_dir: str | Path = "data", run_id: str | None = None) -> dict:
    """只评估已人工冻结的一个候选；已经完成的保留集运行直接回读。"""
    if run_id is None:
        frozen_runs = [
            r for r in list_runs(data_dir) if r.get("frozen") and r.get("verified")
        ]
        if not frozen_runs:
            raise ExperimentError(
                "没有人工冻结的运行，请回到决策页确认推荐候选和理由。"
            )
        run_id = frozen_runs[0]["run_id"]
    path = _run_path(run_id, data_dir)
    with _run_lock(path):
        return _confirm_locked(path, run_id, data_dir)


def _confirm_locked(path: Path, run_id: str, data_dir: str | Path) -> dict:
    """调用方持有运行锁，直至检查、测试、成功或失败封存全部结束。"""
    summary = verified_summary(run_id, data_dir)
    selection = summary.get("frozen_selection")
    if not selection:
        raise ExperimentError("尚未人工冻结，禁止评估测试集；请回到决策页确认。")
    if summary["status"] == "confirmed":
        return summary
    if (path / "test_attempt.json").exists():
        raise ExperimentError(
            "本次保留集评估已尝试但未完成；禁止删除失败种子或重试挑选结果。"
        )
    manifest, records = _snapshot(data_dir)
    if manifest["snapshot_sha256"] != summary["snapshot_sha256"]:
        raise ExperimentError("数据在确认前发生变化。")
    split = _read_json(path / "split_ids.json")
    _assert_disjoint(split, records)
    if split != _split(records):
        raise ExperimentError("持久划分或训练子集与固定规则不一致。")
    by_id = {r.row_id: r for r in records}
    evaluation = [by_id[i] for i in split["test"]]
    candidate = selection["chosen_candidate"]
    attempted_at = _now()
    _save(
        path / "test_attempt.json",
        {"started_at": attempted_at, "candidate": candidate, "status": "started"},
    )
    _log(path, "test_started; frozen_candidate_only")
    rows: list[dict] = []
    started = time.perf_counter()
    try:
        for seed in SEEDS:
            train = [by_id[i] for i in split["samples"]["1000"][str(seed)]]
            result = _fit_evaluate(candidate, train, evaluation)
            rows.append(
                {"candidate": candidate, "budget": 1000, "seed": seed, **result}
            )
            _log(
                path,
                f"test_fit candidate={candidate} budget=1000 seed={seed} seconds={result['total_seconds']:.6f}",
            )
        summary.pop("frozen_selection", None)
        summary.update(
            {
                "status": "confirmed",
                "test_aggregates": _aggregate(rows),
                "test_fit_count": len(rows),
                "test_completed_at": _now(),
            }
        )
        summary["actual_timing"].update(
            {
                "test_wall_seconds": time.perf_counter() - started,
                "test_fit_seconds": sum(r["total_seconds"] for r in rows),
            }
        )
        _csv(path / "test.csv", rows)
        _save(path / "summary.json", summary)
        _save(
            path / "test_attempt.json",
            {
                "started_at": attempted_at,
                "completed_at": _now(),
                "candidate": candidate,
                "status": "success",
            },
        )
        _log(path, "test_complete")
        _seal(path)
    except Exception as exc:
        _save(
            path / "test_attempt.json",
            {
                "started_at": attempted_at,
                "failed_at": _now(),
                "candidate": candidate,
                "status": "failed",
                "error_code": type(exc).__name__,
            },
        )
        _log(path, f"test_failed error_type={type(exc).__name__}; no_retry")
        # 日志是完整性的一部分；失败也保留可核验的验证集结果，但不能声称测试成功。
        _seal(path)
        if isinstance(exc, ExperimentError):
            raise
        raise ExperimentError(
            "保留集评估失败，已保留失败记录；禁止删种子重选赢家。"
        ) from None
    return verified_summary(run_id, data_dir)


def list_runs(data_dir: str | Path = "data") -> list[dict]:
    """历史清单不含原文；核验失败仅给安全状态，不暴露损坏记录的内容。"""
    root = Path(data_dir) / "experiments"
    if not root.is_dir():
        return []
    runs = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            _run_path(path.name, data_dir)
        except ExperimentError:
            continue
        try:
            summary = verified_summary(path.name, data_dir)
            runs.append(
                {
                    "run_id": path.name,
                    "created_at": summary["created_at"],
                    "status": summary["status"],
                    "recommended_candidate": summary["recommended_candidate"],
                    "frozen": "frozen_selection" in summary,
                    "verified": True,
                }
            )
        except ExperimentError:
            runs.append(
                {
                    "run_id": path.name,
                    "created_at": "",
                    "status": "unverified",
                    "recommended_candidate": None,
                    "frozen": False,
                    "verified": False,
                }
            )
    return sorted(
        runs, key=lambda row: (row["created_at"], row["run_id"]), reverse=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="固定 SMS 数据审计、验证集比较与人工冻结后的保留集确认"
    )
    parser.add_argument("command", choices=("audit", "compare", "confirm"))
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--run-id", help="confirm 使用的 UUID；不提供时使用最近已核验且人工冻结的运行"
    )
    args = parser.parse_args()
    try:
        if args.command == "audit":
            result = audit(args.data_dir)
        elif args.command == "compare":
            result = compare(args.data_dir)
        else:
            result = confirm(args.data_dir, args.run_id)
    except ExperimentError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
