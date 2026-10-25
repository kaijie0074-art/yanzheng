"""小型确定性回归；大规模控制流用替身，不重复执行三十次训练。"""

import csv
import hashlib
import json
import math
import multiprocessing
import warnings
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest
from sklearn.exceptions import ConvergenceWarning

from research_app import experiment as exp


def dataset(tmp_path, content, readme=b"Official collection: 5574 messages."):
    case = tmp_path / "case"
    case.mkdir(exist_ok=True)
    (case / "SMSSpamCollection").write_bytes(
        content.encode("utf-8") if isinstance(content, str) else content
    )
    (case / "readme").write_bytes(readme)
    return tmp_path


def record(number, label, text):
    return exp._Record(
        str(number), number, label, text, exp._sha(exp._key(text).encode())
    )


def large_fixture(tmp_path):
    # 1800 个短自造样本足以容纳固定 1000 标签预算；它们不是实际实验数据。
    return dataset(
        tmp_path,
        "".join(
            f"{'spam' if i % 5 == 0 else 'ham'}\tlocalonlysecret sample{i}\n"
            for i in range(1800)
        ),
    )


@pytest.fixture
def mocked_run(tmp_path, monkeypatch):
    root = large_fixture(tmp_path)
    calls = []

    def fit(candidate, train, evaluation):
        calls.append(
            (candidate, [r.row_id for r in train], [r.row_id for r in evaluation])
        )
        truth = [r.label for r in evaluation]
        predictions = truth if candidate == "word_lr" else ["ham"] * len(truth)
        return {
            **exp._metrics(truth, predictions),
            "training_seconds": 0.01,
            "evaluation_seconds": 0.005,
            "total_seconds": 0.015,
        }

    monkeypatch.setattr(exp, "_fit_evaluate", fit)
    summary = exp.compare(root)
    return root, summary, calls


def test_parser_extra_tab_count_difference_and_no_text(tmp_path):
    raw = "ham\tprivate alpha\textra tab\nspam\tprivate beta\n"
    root = dataset(tmp_path, raw, b"Collection 5574; Andr\xe9; actual 2.")
    manifest, records = exp._snapshot(root)
    assert records[0].text == "private alpha\textra tab"
    assert manifest["official_sample_count"] == 5574
    assert manifest["actual_parsed_count"] == 2
    assert manifest["difference_from_official"] == -5572
    assert manifest["snapshot_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert manifest["readme_check"]["status"] == "checked"
    assert manifest["readme_check"]["decoding"] == "iso-8859-1"
    assert manifest["readme_check"]["official_count_mentioned"]
    assert "private" not in (root / "sms_manifest.json").read_text()
    assert records == exp._snapshot(root)[1]


@pytest.mark.parametrize(
    "bad", ["spam no-tab secret", "junk\tsecret", "ham\t   ", "", "  "]
)
def test_invalid_rows_fail_without_exposing_contents(tmp_path, bad):
    root = dataset(tmp_path, f"ham\tvalid example\n{bad}\nspam\tother valid\n")
    with pytest.raises(exp.ExperimentError) as error:
        exp.audit(root)
    manifest = json.loads((root / "sms_manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["error_code"] == "INVALID_ROWS"
    assert 2 in manifest["bad_lines"] + manifest["empty_lines"]
    assert "secret" not in str(error.value)
    assert "secret" not in json.dumps(manifest)
    assert not (root / "experiments").exists()


def test_sms_utf8_strict_even_though_readme_allows_legacy_bytes(tmp_path):
    root = dataset(tmp_path, b"ham\tbad\xffsecret\n", b"5574 Andr\xe9")
    with pytest.raises(exp.ExperimentError, match="UTF-8"):
        exp.audit(root)
    manifest = json.loads((root / "sms_manifest.json").read_text())
    assert manifest["error_code"] == "INVALID_UTF8"
    assert "secret" not in json.dumps(manifest)


def test_duplicate_first_and_conflicting_entire_group_quarantine(tmp_path):
    root = dataset(
        tmp_path,
        "ham\tＳＡＭＥ   message\nham\tsame message\nspam\tconflict text\nham\tCONFLICT\ttext\nspam\tconflict text\nspam\tunique offer\n",
    )
    manifest, records = exp._snapshot(root)
    assert [r.line_number for r in records] == [1, 6]
    assert manifest["actual_parsed_count"] == 6
    assert manifest["duplicate_removed_count"] == 1
    assert manifest["conflict_group_count"] == 1
    assert manifest["conflict_row_count"] == 3
    assert manifest["retained_label_counts"] == {"ham": 1, "spam": 1}
    assert manifest["conflict_groups"][0]["line_numbers"] == [3, 4, 5]


def test_split_ratios_nested_budgets_reproducibility_and_key_disjointness(tmp_path):
    _, records = exp._snapshot(large_fixture(tmp_path))
    split = exp._split(records)
    assert [len(split[k]) for k in ("train_pool", "validation", "test")] == [
        1080,
        360,
        360,
    ]
    assert split == exp._split(records)
    by_id = {r.row_id: r for r in records}
    for seed in exp.SEEDS:
        previous = set()
        for budget in exp.BUDGETS:
            ids = split["samples"][str(budget)][str(seed)]
            assert len(ids) == len(set(ids)) == budget
            assert previous <= set(ids) <= set(split["train_pool"])
            assert sum(by_id[i].label == "spam" for i in ids) == budget // 5
            previous = set(ids)
    assert split["samples"]["200"]["0"] != split["samples"]["200"]["1"]
    exp._assert_disjoint(split, records)
    bad = dict(split)
    bad["validation"] = [split["train_pool"][0], *split["validation"][1:]]
    with pytest.raises(exp.ExperimentError, match="泄漏"):
        exp._assert_disjoint(bad, records)
    # 不同行 id 也必须检查规范化正文键，而不只检查 id。
    duplicate_key = replace(
        by_id[split["validation"][0]], key_hash=by_id[split["train_pool"][0]].key_hash
    )
    changed = [
        duplicate_key if r.row_id == duplicate_key.row_id else r for r in records
    ]
    with pytest.raises(exp.ExperimentError, match="泄漏"):
        exp._assert_disjoint(split, changed)


@pytest.mark.parametrize("candidate", exp.CANDIDATES)
def test_actual_pipeline_fits_only_training_vocabulary(candidate, monkeypatch):
    train = [
        record(1, "ham", "trainham meeting"),
        record(2, "ham", "trainham lunch"),
        record(3, "spam", "trainspam prize"),
        record(4, "spam", "trainspam claim"),
    ]
    evaluation = [
        record(5, "ham", "heldoutvalidationtoken"),
        record(6, "spam", "heldouttesttoken"),
    ]
    model = exp._pipeline(candidate)
    monkeypatch.setattr(exp, "_pipeline", lambda _: model)
    result = exp._fit_evaluate(candidate, train, evaluation)
    vocabulary = model.named_steps["tfidf"].vocabulary_
    assert "trainham" in vocabulary and "trainspam" in vocabulary
    assert "heldoutvalidationtoken" not in vocabulary
    assert "heldouttesttoken" not in vocabulary
    assert result["ham_support"] == result["spam_support"] == 1
    assert result["total_seconds"] >= result["training_seconds"] >= 0
    assert model.named_steps["tfidf"].transform(["trainham"]).format == "csr"


def test_metrics_positive_class_and_false_positive_denominator():
    result = exp._metrics(
        ["ham", "ham", "ham", "spam", "spam"], ["spam", "ham", "ham", "spam", "ham"]
    )
    assert result["spam_precision"] == 0.5
    assert result["spam_recall"] == 0.5
    assert result["spam_f1"] == 0.5
    assert result["ham_false_positive_rate"] == pytest.approx(1 / 3)
    assert result["macro_f1"] == pytest.approx((2 / 3 + 0.5) / 2)
    assert result["ham_support"] == 3 and result["spam_support"] == 2


def aggregates(nb_f1, lr_f1, nb_fp=0.1, lr_fp=0.1):
    return [
        {
            "candidate": candidate,
            "budget": 1000,
            "metrics": {
                "spam_f1": {"mean": f1},
                "ham_false_positive_rate": {"mean": fp},
            },
        }
        for candidate, f1, fp in (("word_nb", nb_f1, nb_fp), ("word_lr", lr_f1, lr_fp))
    ]


@pytest.mark.parametrize(
    "values,winner",
    [
        ((0.8, 0.9), "word_lr"),
        ((0.9, 0.8), "word_nb"),
        ((0.8, 0.8, 0.2, 0.1), "word_lr"),
        ((0.8, 0.8, 0.1, 0.2), "word_nb"),
        ((0.8, 0.8), "word_nb"),
        ((0.8, 0.8 + 5e-13, 0.1, 0.2), "word_nb"),
    ],
)
def test_fixed_validation_tie_break(values, winner):
    assert exp._recommend(aggregates(*values)) == winner


def test_sample_standard_deviation_and_paired_differences():
    assert exp._stats([1.0, 2.0, 3.0])["sample_std"] == 1.0
    rows = [
        {
            "candidate": candidate,
            "budget": budget,
            "seed": seed,
            **{m: (seed / 10 if candidate == "word_lr" else 0.0) for m in exp.METRICS},
        }
        for budget in exp.BUDGETS
        for seed in exp.SEEDS
        for candidate in exp.CANDIDATES
    ]
    pair = exp._paired(rows)[0]["metrics"]["spam_f1"]
    assert pair["mean"] == pytest.approx(0.2)
    assert pair["sample_std"] == pytest.approx(math.sqrt(0.025))
    assert pair["by_seed"] == {str(i): i / 10 for i in exp.SEEDS}


def test_compare_thirty_fits_shared_samples_and_no_test_access(mocked_run):
    root, summary, calls = mocked_run
    path = root / "experiments" / summary["run_id"]
    split = json.loads((path / "split_ids.json").read_text())
    assert summary["status"] == "validation_complete"
    assert summary["validation_fit_count"] == len(calls) == 30
    assert summary["recommended_candidate"] == "word_lr"
    assert len(summary["validation_aggregates"]) == 6
    assert len(summary["paired_differences"]) == 3
    for first, second in zip(calls[::2], calls[1::2]):
        assert first[0] == "word_nb" and second[0] == "word_lr"
        assert first[1:] == second[1:]
        assert set(first[1]) <= set(split["train_pool"])
        assert set(first[2]) == set(split["validation"])
        assert not set(first[1] + first[2]) & set(split["test"])
    assert not (path / "test.csv").exists()
    assert not (path / "frozen_selection.json").exists()
    with (path / "validation.csv").open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 30
    for output in path.iterdir():
        assert "localonlysecret" not in output.read_text()
    assert (
        exp.verified_summary(summary["run_id"], root)["recommended_candidate"]
        == "word_lr"
    )
    assert exp.list_runs(root)[0]["verified"]


def test_explicit_freeze_then_five_selected_test_fits_and_idempotence(mocked_run):
    root, summary, calls = mocked_run
    run_id = summary["run_id"]
    with pytest.raises(exp.ExperimentError, match="人工冻结"):
        exp.confirm(root, run_id)
    assert len(calls) == 30
    with pytest.raises(exp.ExperimentError, match="非空理由"):
        exp.freeze_selection(run_id, reason=" ", study_revision=1, data_dir=root)
    frozen = exp.freeze_selection(
        run_id, reason="根据验证结果采用固定推荐", study_revision=2, data_dir=root
    )
    assert frozen["chosen_candidate"] == "word_lr"
    assert frozen["study_revision"] == 2 and frozen["confirmed_at"]
    with pytest.raises(exp.ExperimentError, match="已经冻结"):
        exp.freeze_selection(run_id, reason="换理由", study_revision=2, data_dir=root)
    confirmed = exp.confirm(root)
    assert confirmed["status"] == "confirmed"
    assert confirmed["test_fit_count"] == 5
    assert confirmed["frozen_selection"] == frozen
    assert len(calls) == 35
    path = root / "experiments" / run_id
    split = json.loads((path / "split_ids.json").read_text())
    for candidate, train_ids, eval_ids in calls[30:]:
        assert candidate == "word_lr"
        assert len(train_ids) == 1000
        assert set(eval_ids) == set(split["test"])
    with (path / "test.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5 and {r["candidate"] for r in rows} == {"word_lr"}
    assert exp.confirm(root, run_id) == confirmed
    assert len(calls) == 35


@pytest.mark.parametrize(
    "file",
    [
        "summary.json",
        "validation.csv",
        "config.json",
        "split_ids.json",
        "manifest.json",
        "run.log",
    ],
)
def test_changed_run_artifact_refused(mocked_run, file):
    root, summary, _ = mocked_run
    path = root / "experiments" / summary["run_id"] / file
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(exp.ExperimentError, match="哈希"):
        exp.verified_summary(summary["run_id"], root)
    assert not exp.list_runs(root)[0]["verified"]


def test_changed_raw_bytes_refused(mocked_run):
    root, summary, _ = mocked_run
    raw = root / "case" / "SMSSpamCollection"
    raw.write_bytes(raw.read_bytes() + b"ham\textra row\n")
    with pytest.raises(exp.ExperimentError, match="快照哈希"):
        exp.freeze_selection(
            summary["run_id"], reason="人工确认", study_revision=1, data_dir=root
        )


def test_changed_freeze_and_changed_dependencies_refused(mocked_run, monkeypatch):
    root, summary, _ = mocked_run
    run_id = summary["run_id"]
    original = exp._config()
    monkeypatch.setattr(exp, "_config", lambda: {**original, "python": "changed"})
    with pytest.raises(exp.ExperimentError, match="配置哈希"):
        exp.verified_summary(run_id, root)
    monkeypatch.setattr(exp, "_config", lambda: original)
    exp.freeze_selection(run_id, reason="人工确认", study_revision=1, data_dir=root)
    frozen = root / "experiments" / run_id / "frozen_selection.json"
    value = json.loads(frozen.read_text())
    value["chosen_candidate"] = "word_nb"
    frozen.write_text(json.dumps(value))
    with pytest.raises(exp.ExperimentError, match="哈希"):
        exp.confirm(root, run_id)


def test_failed_holdout_never_writes_test_csv_or_retries(mocked_run, monkeypatch):
    root, summary, _ = mocked_run
    run_id = summary["run_id"]
    exp.freeze_selection(run_id, reason="人工确认", study_revision=1, data_dir=root)
    attempts = []

    def fail(*args):
        attempts.append(args[0])
        raise exp.ExperimentError("分类器未收敛")

    monkeypatch.setattr(exp, "_fit_evaluate", fail)
    with pytest.raises(exp.ExperimentError, match="未收敛"):
        exp.confirm(root, run_id)
    path = root / "experiments" / run_id
    assert not (path / "test.csv").exists()
    assert json.loads((path / "test_attempt.json").read_text())["status"] == "failed"
    assert exp.verified_summary(run_id, root)["status"] == "validation_complete"
    with pytest.raises(exp.ExperimentError, match="已尝试"):
        exp.confirm(root, run_id)
    assert attempts == ["word_lr"]
    (path / "test_attempt.json").unlink()
    with pytest.raises(exp.ExperimentError, match="不一致"):
        exp.confirm(root, run_id)


def _experiment_worker(
    root, run_id, action, result, fit_count, ready, release, fail_on_fit
):
    """spawn 子进程独立加载实现；事件把竞争固定在旧实现的检查/写入间隙。"""
    original_verify = exp.verified_summary
    original_snapshot = exp._snapshot

    def pause():
        if ready is not None:
            ready.set()
            if not release.wait(60):
                raise RuntimeError("并发测试释放事件超时")

    def verify(*args, **kwargs):
        summary = original_verify(*args, **kwargs)
        if action == "freeze":
            pause()
        return summary

    def snapshot(*args, **kwargs):
        # confirm 已通过 test_attempt 不存在门禁，但尚未写入占用记录。
        pause()
        return original_snapshot(*args, **kwargs)

    def fit(candidate, train, evaluation):
        with fit_count.get_lock():
            fit_count.value += 1
            attempt = fit_count.value
        if fail_on_fit == attempt:
            raise exp.ExperimentError("并发回归中的固定拟合失败")
        truth = [r.label for r in evaluation]
        return {
            **exp._metrics(truth, truth),
            "training_seconds": 0.01,
            "evaluation_seconds": 0.005,
            "total_seconds": 0.015,
        }

    try:
        with (
            patch.object(exp, "verified_summary", verify),
            patch.object(exp, "_snapshot", snapshot),
            patch.object(exp, "_fit_evaluate", fit),
        ):
            if action == "freeze":
                value = exp.freeze_selection(
                    run_id,
                    reason="首个冻结" if ready is not None else "竞争理由",
                    study_revision=2 if ready is not None else 9,
                    data_dir=root,
                )
            else:
                value = exp.confirm(root, run_id)
        result.put(("ok", value))
    except exp.ExperimentError as exc:
        result.put(("error", str(exc)))
    except Exception as exc:
        result.put(("unexpected", repr(exc)))


@contextmanager
def _experiment_process(
    ctx, root, run_id, action, fit_count, *, ready=None, release=None, fail_on_fit=None
):
    result = ctx.Queue()
    process = ctx.Process(
        target=_experiment_worker,
        args=(root, run_id, action, result, fit_count, ready, release, fail_on_fit),
    )
    process.start()
    try:
        yield result
    finally:
        if release is not None:
            release.set()
        process.join(60)
        if process.is_alive():
            process.terminate()
            process.join(10)
        result.close()
        result.join_thread()
        assert process.exitcode == 0


@pytest.mark.parametrize(
    "first_action,second_action",
    [
        ("freeze", "freeze"),
        ("freeze", "confirm"),
        ("confirm", "freeze"),
        ("confirm", "confirm"),
    ],
)
def test_cross_process_freeze_and_confirm_share_exclusive_transaction(
    mocked_run, first_action, second_action
):
    root, summary, _ = mocked_run
    run_id = summary["run_id"]
    path = root / "experiments" / run_id
    if first_action == "confirm":
        frozen = exp.freeze_selection(
            run_id, reason="已有人工确认", study_revision=1, data_dir=root
        )
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    fit_count = ctx.Value("i", 0)
    with _experiment_process(
        ctx, root, run_id, first_action, fit_count, ready=ready, release=release
    ) as first:
        assert ready.wait(60), "第一个事务未到达确定性暂停点"
        with _experiment_process(ctx, root, run_id, second_action, fit_count) as second:
            status, value = second.get(timeout=60)
            assert status == "error" and "运行处理中" in value
        assert fit_count.value == 0
        release.set()
        status, value = first.get(timeout=60)
        assert status == "ok"

    verified = exp.verified_summary(run_id, root)
    integrity = exp._read_json(path / "integrity.json")
    assert (path / ".transaction.lock").is_file()
    assert ".transaction.lock" not in integrity
    assert set(integrity) <= {*exp.BASE_FILES, *exp.OPTIONAL_FILES}
    frozen_bytes = (path / "frozen_selection.json").read_bytes()
    if first_action == "freeze":
        assert verified["status"] == "validation_complete"
        assert verified["frozen_selection"] == value
        assert value["reason"] == "首个冻结" and value["study_revision"] == 2
        assert fit_count.value == 0
        with _experiment_process(ctx, root, run_id, "freeze", fit_count) as retry:
            status, value = retry.get(timeout=60)
            assert status == "error" and "已经冻结" in value
    else:
        assert verified["status"] == "confirmed"
        assert verified["frozen_selection"] == frozen
        assert verified["test_fit_count"] == fit_count.value == 5
        with (path / "test.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 5
        assert {int(row["seed"]) for row in rows} == set(exp.SEEDS)
        assert {row["candidate"] for row in rows} == {frozen["chosen_candidate"]}
        sealed_bytes = (path / "integrity.json").read_bytes()
        with _experiment_process(
            ctx, root, run_id, "confirm", fit_count, fail_on_fit=6
        ) as retry:
            status, value = retry.get(timeout=60)
            assert status == "ok" and value == verified
        assert fit_count.value == 5
        assert (path / "integrity.json").read_bytes() == sealed_bytes
    assert (path / "frozen_selection.json").read_bytes() == frozen_bytes


def test_cross_process_failed_holdout_cannot_be_overwritten_by_success(mocked_run):
    root, summary, _ = mocked_run
    run_id = summary["run_id"]
    path = root / "experiments" / run_id
    frozen = exp.freeze_selection(
        run_id, reason="固定失败回归", study_revision=1, data_dir=root
    )
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    fit_count = ctx.Value("i", 0)
    with _experiment_process(
        ctx,
        root,
        run_id,
        "confirm",
        fit_count,
        ready=ready,
        release=release,
        fail_on_fit=3,
    ) as first:
        assert ready.wait(60), "第一个事务未到达确定性暂停点"
        with _experiment_process(ctx, root, run_id, "confirm", fit_count) as second:
            status, value = second.get(timeout=60)
            assert status == "error" and "运行处理中" in value
        release.set()
        status, value = first.get(timeout=60)
        assert status == "error" and "固定拟合失败" in value
    assert fit_count.value == 3
    assert not (path / "test.csv").exists()
    assert exp._read_json(path / "test_attempt.json")["status"] == "failed"
    verified = exp.verified_summary(run_id, root)
    assert verified["status"] == "validation_complete"
    assert verified["frozen_selection"] == frozen
    preserved = {
        name: (path / name).read_bytes()
        for name in ("test_attempt.json", "summary.json", "integrity.json", "run.log")
    }
    with _experiment_process(ctx, root, run_id, "confirm", fit_count) as retry:
        status, value = retry.get(timeout=60)
        assert status == "error" and "已尝试" in value
    assert fit_count.value == 3
    assert not (path / "test.csv").exists()
    assert {name: (path / name).read_bytes() for name in preserved} == preserved


def test_convergence_warning_and_empty_vocabulary_are_hard_failures(monkeypatch):
    train = [record(1, "ham", "ham content"), record(2, "spam", "spam content")]

    class NonConvergent:
        def fit(self, *_):
            warnings.warn("private details", ConvergenceWarning)

    monkeypatch.setattr(exp, "_pipeline", lambda _: NonConvergent())
    with pytest.raises(exp.ExperimentError, match="未收敛"):
        exp._fit_evaluate("word_lr", train, train)

    class EmptyVocabulary:
        def fit(self, *_):
            raise ValueError("empty vocabulary private details")

    monkeypatch.setattr(exp, "_pipeline", lambda _: EmptyVocabulary())
    with pytest.raises(exp.ExperimentError) as error:
        exp._fit_evaluate("word_nb", train, train)
    assert "private details" not in str(error.value)


def test_run_id_path_traversal_is_rejected(tmp_path):
    with pytest.raises(exp.ExperimentError, match="UUID"):
        exp.verified_summary("../outside", tmp_path)


def test_cli_audit_safe_output_and_confirm_requires_freeze(
    tmp_path, monkeypatch, capsys
):
    dataset(tmp_path, "ham\tprivate first\nspam\tprivate second\n")
    monkeypatch.setattr(
        "sys.argv", ["experiment", "audit", "--data-dir", str(tmp_path)]
    )
    assert exp.main() == 0
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["actual_parsed_count"] == 2
    monkeypatch.setattr(
        "sys.argv", ["experiment", "confirm", "--data-dir", str(tmp_path)]
    )
    assert exp.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_readme_comma_counts_and_discrepancy_are_not_silently_reconciled(tmp_path):
    readme = (
        b"It contains 5,574 messages, tagged as ham or spam.\n"
        b"It has a total of 4,827 SMS legitimate messages (86.6%) "
        b"and a total of 747 (13.4%) spam messages.\n"
        b"spam   example secret 123456\n"
    )
    root = dataset(tmp_path, "ham\tone local text\nspam\tsecond local text\n", readme)
    check = exp.audit(root)["readme_check"]
    assert check["declared_counts"] == {"total": 5574, "ham": 4827, "spam": 747}
    assert check["actual_minus_declared"] == {
        "total": -5572,
        "ham": -4826,
        "spam": -746,
    }
    assert 123456 not in check["numeric_mentions"]
    assert "secret" not in json.dumps(check)


def test_failed_validation_retains_failure_without_success_metrics(
    tmp_path, monkeypatch
):
    root = large_fixture(tmp_path)

    def failed_fit(*_):
        raise exp.ExperimentError("空词表")

    monkeypatch.setattr(exp, "_fit_evaluate", failed_fit)
    with pytest.raises(exp.ExperimentError, match="空词表"):
        exp.compare(root)
    directories = list((root / "experiments").iterdir())
    assert len(directories) == 1
    summary = json.loads((directories[0] / "summary.json").read_text())
    assert summary["status"] == "failed"
    assert summary["recommended_candidate"] is None
    assert summary["validation_aggregates"] == []
    assert not (directories[0] / "validation.csv").exists()
    assert not (directories[0] / "test.csv").exists()
    with pytest.raises(exp.ExperimentError):
        exp.verified_summary(summary["run_id"], root)
