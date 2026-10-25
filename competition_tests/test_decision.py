"""离线业务契约测试：自造PDF和受控后端不代表真实网关推理。"""

import copy
import json
import os
import shutil
from io import BytesIO

import pytest
from pydantic import BaseModel, ValidationError
from reportlab.pdfgen.canvas import Canvas

from research_app import documents, evidence, experiment, export
from research_app.backends import BackendError
from research_app.decision import (
    SMS_METHODS,
    decision_context,
    generate_candidates,
    list_candidate_runs,
    list_decisions,
    save_decision,
)
from research_app.documents import import_pdf
from research_app.evidence import analyze_pages, edit_card, get_card, review_card
from research_app.models import BriefContent, ModelConfig, ModelReply, now
from research_app.storage import Store
from research_app.workflow import SMS_QUESTION, confirm_brief


class UncheckedCandidates(BaseModel):
    candidates: list[dict]


class ControlledBackend:
    """仅返回测试控制数据；不调用任何网络或本机账号。"""

    config = ModelConfig()

    def __init__(self, output, *, unchecked=False, before_reply=None):
        self.output = output
        self.unchecked = unchecked
        self.before_reply = before_reply
        self.requests = []

    def complete_json(self, **request):
        self.requests.append(request)
        if self.before_reply:
            self.before_reply()
        value = (
            UncheckedCandidates.model_validate(self.output)
            if self.unchecked
            else request["schema"].model_validate(self.output)
        )
        return ModelReply(
            value=value,
            requested_model="test/unit",
            actual_model="test/unit",
            backend="controlled_test",
            usage=None,
            called_at=now(),
            text=json.dumps(self.output),
        )


def study(question="比较英文短信分类基线"):
    return BriefContent(
        question=question,
        population_or_object="公开历史英文短信",
        constraints="有限标签",
        available_resources="本机CPU",
        validation_goal="验证集spam F1与ham误报率",
    )


def candidate_output(ids, *, sms=True):
    methods = (
        SMS_METHODS if sms else {"written_a": "书面方法甲", "written_b": "书面方法乙"}
    )
    return {
        "candidates": [
            {
                "candidate_id": cid,
                "method": method,
                "evidence_ids": list(ids),
                "conditions": "仅限当前语料",
                "feasibility": "本机CPU可运行",
                "minimal_validation": "固定划分后比较验证指标",
                "objections": "不能外推现代中文短信",
            }
            for cid, method in methods.items()
        ]
    }


@pytest.fixture
def prepared(tmp_path, attach_confirmed_sms):
    store = Store(tmp_path)
    confirm_brief(store, study(), ["SMS classification"])
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(50, 750, "Simple baselines need evaluation on the target corpus.")
    canvas.save()
    source = import_pdf(
        store,
        buffer.getvalue(),
        title="Synthetic test paper",
        authors=["Test Author"],
        year=2026,
        official_url="https://example.org/synthetic-test",
    )
    store.set_bibliography(source["id"], True)
    attach_confirmed_sms(store)
    evidence = {
        "cards": [
            {
                "claim": "需要在目标语料验证",
                "source_id": source["id"],
                "page_number": 1,
                "quote": "Simple baselines need evaluation on the target corpus.",
                "conditions": "仅限目标语料",
                "limitations": "不提供SMS性能保证",
                "ai_relation": "contradicts" if index == 3 else "supports",
            }
            for index in range(5)
        ]
    }
    cards = analyze_pages(
        store, ControlledBackend(evidence), [(source["id"], 1)], rights_confirmed=True
    )
    for card, status in zip(
        cards,
        [
            "supported",
            "unreviewed",
            "partially_supported",
            "unsupported",
            "insufficient",
        ],
    ):
        if status != "unreviewed":
            review_card(
                store,
                card["id"],
                status,
                "测试复核：目标语料需要独立验证",
                pdf_opened=True,
            )
    return store, source, cards


def generated(prepared, *, sms=True):
    store, _, cards = prepared
    backend = ControlledBackend(candidate_output([cards[0]["id"]], sms=sms))
    run = generate_candidates(store, backend, sms_case=sms)
    if sms:
        summary = experiment.list_runs(store.data_dir)[0]
        run["test_experiment_run_id"] = summary["run_id"]
        run["test_recommended_candidate"] = summary["recommended_candidate"]
    return run


def save(store, run, ids, **changes):
    arguments = dict(
        candidate_run_id=run["id"],
        chosen_candidate=run.get(
            "test_recommended_candidate", run["candidates"][0]["candidate_id"]
        ),
        reason="人工选择：满足当前资源约束，仍须遵守迁移边界",
        evidence_ids=ids,
        final=True,
    )
    if run.get("sms_case") and changes.get("final", True):
        arguments["experiment_run_id"] = run["test_experiment_run_id"]
    arguments.update(changes)
    return save_decision(store, **arguments)


def test_generation_uses_only_current_eligible_and_preserves_disagreements(prepared):
    store, _, cards = prepared
    backend = ControlledBackend(candidate_output([cards[0]["id"]]))
    run = generate_candidates(store, backend, sms_case=True)
    payload = backend.requests[0]["payload"]
    assert [card["id"] for card in payload["evidence"]] == [cards[0]["id"]]
    assert set(payload) == {"brief", "evidence", "fixed_methods"}
    assert all("review_status" not in card for card in payload["evidence"])
    assert payload["fixed_methods"] == SMS_METHODS
    assert run["study_revision"] == 1
    assert list_candidate_runs(Store(store.data_dir)) == [run]
    audit = store.rows("SELECT * FROM model_runs WHERE id=?", (run["id"],))[0]
    assert audit["status"] == "success"
    assert json.loads(audit["input_refs"])["evidence_ids"] == [cards[0]["id"]]
    assert audit["actual_model"] == "test/unit"
    context = decision_context(store)
    assert {card["id"] for card in context["disagreements"]} == {
        card["id"] for card in cards[1:]
    }
    assert {card["id"] for card in context["limitations"]} == {
        card["id"] for card in cards
    }
    assert context["study_revision"] == 1


def test_no_eligible_cards_never_calls_model_and_context_marks_incomplete(prepared):
    store, _, cards = prepared
    review_card(
        store, cards[0]["id"], "unsupported", "测试复核：不支持", pdf_opened=True
    )
    backend = ControlledBackend(candidate_output([]))
    with pytest.raises(ValueError, match="没有合格证据"):
        generate_candidates(store, backend)
    assert backend.requests == []
    assert list_candidate_runs(store) == []
    assert decision_context(store)["verification_status"] == "未完成核验"
    assert len(decision_context(store)["disagreements"]) == 5


def test_missing_brief_never_calls_model(tmp_path):
    store = Store(tmp_path)
    backend = ControlledBackend(candidate_output([]))
    with pytest.raises(ValueError, match="确认研究问题"):
        generate_candidates(store, backend)
    assert backend.requests == []
    assert decision_context(store)["study_revision"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate_id",
        "foreign_id",
        "unsupported_id",
        "duplicate_evidence",
        "third_algorithm",
        "changed_method",
        "empty_method",
        "review_status",
        "extra_candidate",
    ],
)
def test_invalid_candidate_cannot_gain_persisted_authority(prepared, kind):
    store, _, cards = prepared
    output = candidate_output([cards[0]["id"]])
    first, second = output["candidates"]
    if kind == "duplicate_id":
        second["candidate_id"] = first["candidate_id"]
    elif kind == "foreign_id":
        first["evidence_ids"] = ["never-supplied-card"]
    elif kind == "unsupported_id":
        first["evidence_ids"] = [cards[3]["id"]]
    elif kind == "duplicate_evidence":
        first["evidence_ids"] *= 2
    elif kind == "third_algorithm":
        first["candidate_id"] = "svm"
    elif kind == "changed_method":
        first["method"] = "执行第三种分类器"
    elif kind == "empty_method":
        first["method"] = " "
    elif kind == "review_status":
        first["review_status"] = "supported"
    elif kind == "extra_candidate":
        output["candidates"].append(copy.deepcopy(first))
    backend = ControlledBackend(output, unchecked=True)
    with pytest.raises((ValueError, ValidationError)):
        generate_candidates(store, backend, sms_case=True)
    assert list_candidate_runs(store) == []
    audit = store.rows("SELECT * FROM model_runs WHERE task='candidates'")[0]
    assert audit["status"] == "failed"
    assert audit["error_type"] == "CANDIDATE_VALIDATION"
    assert audit["output_text"] == json.dumps(output)


@pytest.mark.parametrize("change", ["brief", "review", "pdf"])
def test_candidate_generation_rechecks_upstream_after_model_reply(prepared, change):
    store, source, cards = prepared

    def update():
        if change == "brief":
            confirm_brief(store, study("另一个研究问题"), ["different query"])
        elif change == "pdf":
            (store.papers_dir / f"{source['sha256']}.pdf").unlink()
        else:
            review_card(
                store,
                cards[0]["id"],
                "unsupported",
                "重新判断：不支持",
                pdf_opened=True,
            )

    backend = ControlledBackend(candidate_output([cards[0]["id"]]), before_reply=update)
    with pytest.raises(ValueError, match="生成期间"):
        generate_candidates(store, backend, sms_case=True)
    assert list_candidate_runs(store) == []
    assert (
        store.rows("SELECT status FROM model_runs WHERE task='candidates'")[0]["status"]
        == "failed"
    )


def test_backend_failure_audits_without_losing_material(prepared):
    store, source, _ = prepared

    class FailingBackend:
        config = ModelConfig()

        def complete_json(self, **kwargs):
            raise BackendError("RATE_LIMIT", "受控限流错误")

    with pytest.raises(BackendError):
        generate_candidates(store, FailingBackend())
    assert list_candidate_runs(store) == []
    assert store.get_source(source["id"])["bibliography_status"] == "confirmed"
    assert (
        store.rows("SELECT error_type FROM model_runs WHERE task='candidates'")[0][
            "error_type"
        ]
        == "RATE_LIMIT"
    )


def test_final_persists_candidate_link_and_draft_can_have_no_evidence(prepared):
    store, _, cards = prepared
    run = generated(prepared)
    final = save(store, run, [cards[0]["id"]])
    draft = save(store, run, [], final=False)
    assert final["status"] == "final" and draft["status"] == "draft"
    assert draft["evidence_ids"] == []
    assert final["candidate_run_id"] == run["id"]
    assert list_decisions(Store(store.data_dir)) == [final, draft]


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": " "},
        {"evidence_ids": []},
        {"chosen_candidate": "not-a-candidate"},
        {"candidate_run_id": "missing-run"},
    ],
)
def test_formal_human_reason_candidate_and_minimum_evidence_gates(prepared, changes):
    store, _, cards = prepared
    changes = dict(changes)
    run = generated(prepared)
    ids = changes.pop("evidence_ids", [cards[0]["id"]])
    with pytest.raises(ValueError):
        save(store, run, ids, **changes)
    assert list_decisions(store) == []


def test_decision_must_use_selected_candidates_evidence_not_just_any_eligible_card(
    prepared,
):
    store, _, cards = prepared
    review_card(
        store, cards[1]["id"], "supported", "测试复核：确有支持", pdf_opened=True
    )
    run = generated(prepared)
    for ids in [[cards[1]["id"]], [cards[0]["id"], cards[0]["id"]], ["missing-card"]]:
        with pytest.raises(ValueError):
            save(store, run, ids)
    assert list_decisions(store) == []


@pytest.mark.parametrize("mutation", ["review", "bibliography", "edit", "page_text"])
def test_upstream_loss_blocks_final_and_preserves_history(prepared, mutation):
    store, source, cards = prepared
    run = generated(prepared)
    saved = save(store, run, [cards[0]["id"]])
    if mutation == "review":
        review_card(
            store, cards[0]["id"], "unsupported", "测试复核：撤回支持", pdf_opened=True
        )
    elif mutation == "bibliography":
        store.set_bibliography(source["id"], False)
    elif mutation == "edit":
        content = get_card(store, cards[0]["id"])["content"]
        edit_card(store, cards[0]["id"], {**content, "claim": "改写过的主张"})
    else:
        with store.connection() as db:
            db.execute(
                "UPDATE pages SET text='different text' WHERE source_id=?",
                (source["id"],),
            )
    with pytest.raises(ValueError, match="全部通过"):
        save(store, run, [cards[0]["id"]])
    decisions = list_decisions(store)
    assert len(decisions) == 1 and decisions[0]["id"] == saved["id"]
    if mutation != "page_text":
        assert decisions[0]["status"] == "draft" and decisions[0]["invalidated_at"]
    assert list_candidate_runs(store)[0]["id"] == run["id"]


def test_old_revision_candidates_cannot_finalize_and_old_cards_never_feed_new_model(
    prepared,
):
    store, _, cards = prepared
    run = generated(prepared)
    final = save(store, run, [cards[0]["id"]])
    confirm_brief(store, study("新的研究问题"), ["new question"])
    historical = list_decisions(store)[0]
    assert historical["id"] == final["id"] and historical["status"] == "draft"
    assert historical["invalidated_at"]
    with pytest.raises(ValueError, match="历史修订"):
        save(store, run, [cards[0]["id"]])
    backend = ControlledBackend(candidate_output([cards[0]["id"]]))
    with pytest.raises(ValueError, match="没有合格证据"):
        generate_candidates(store, backend)
    assert backend.requests == []
    assert decision_context(store)["disagreements"] == []
    historical_draft = save(store, run, [], final=False)
    assert (
        historical_draft["study_revision"] == 1
        and historical_draft["status"] == "draft"
    )


def confirmed_summary():
    return {
        "status": "confirmed",
        "recommended_candidate": "word_nb",
        "frozen_selection": {
            "study_revision": 1,
            "chosen_candidate": "word_nb",
            "reason": "人工冻结",
            "confirmed_at": now(),
        },
    }


def test_experiment_summary_is_read_via_verified_boundary(prepared, monkeypatch):
    from research_app import experiment

    store, _, cards = prepared
    run = generated(prepared)
    calls = []

    def verified(run_id, *, data_dir):
        calls.append((run_id, data_dir))
        return confirmed_summary()

    monkeypatch.setattr(experiment, "verified_summary", verified)
    decision = save(
        store,
        run,
        [cards[0]["id"]],
        experiment_run_id="unit-run",
        chosen_candidate="word_nb",
    )
    assert calls == [("unit-run", store.data_dir)]
    assert decision["experiment_run_id"] == "unit-run" and decision["status"] == "final"
    assert "test_aggregates" not in decision


@pytest.mark.parametrize(
    "mutation",
    [
        "not_confirmed",
        "old_revision",
        "missing_frozen",
        "wrong_frozen_winner",
        "wrong_champion",
    ],
)
def test_experiment_final_requires_confirmed_current_frozen_winner(
    prepared, monkeypatch, mutation
):
    from research_app import experiment

    store, _, cards = prepared
    run = generated(prepared)
    summary = confirmed_summary()
    if mutation == "not_confirmed":
        summary["status"] = "validation_complete"
    elif mutation == "old_revision":
        summary["frozen_selection"]["study_revision"] = 0
    elif mutation == "missing_frozen":
        del summary["frozen_selection"]
    elif mutation == "wrong_frozen_winner":
        summary["frozen_selection"]["chosen_candidate"] = "word_lr"
    else:
        summary["recommended_candidate"] = "word_lr"
    monkeypatch.setattr(experiment, "verified_summary", lambda *args, **kwargs: summary)
    with pytest.raises(ValueError):
        save(
            store,
            run,
            [cards[0]["id"]],
            experiment_run_id="unit-run",
            chosen_candidate="word_nb",
        )
    assert list_decisions(store) == []


def test_hash_verification_failure_never_saves_experiment_decision(
    prepared, monkeypatch
):
    from research_app import experiment

    store, _, cards = prepared
    run = generated(prepared)

    def changed_hash(*args, **kwargs):
        raise ValueError("数据或配置哈希不一致")

    monkeypatch.setattr(experiment, "verified_summary", changed_hash)
    with pytest.raises(ValueError, match="哈希"):
        save(store, run, [cards[0]["id"]], experiment_run_id="unit-run")
    assert list_decisions(store) == []


def test_non_sms_written_candidates_cannot_attach_fixed_experiment(
    prepared, monkeypatch
):
    from research_app import experiment

    store, _, cards = prepared
    run = generated(prepared, sms=False)
    decision = save(store, run, [cards[0]["id"]])
    assert decision["chosen_candidate"] == "written_a"
    assert decision["experiment_run_id"] is None
    assert export.process_snapshot(store)["decisions"][0]["effective_status"] == "final"

    def unexpected(*args, **kwargs):
        pytest.fail("非SMS方案不得调用固定实验摘要")

    monkeypatch.setattr(experiment, "verified_summary", unexpected)
    with pytest.raises(ValueError, match="其他研究问题"):
        save(store, run, [cards[0]["id"]], experiment_run_id="unit-run")


def test_supported_counterargument_remains_visible_in_disagreements(prepared):
    store, _, cards = prepared
    review_card(
        store,
        cards[3]["id"],
        "supported",
        "原文支持一条反对意见，而非候选优势",
        pdf_opened=True,
    )
    context = decision_context(store)
    assert cards[3]["id"] in {card["id"] for card in context["eligible_cards"]}
    assert cards[3]["id"] in {card["id"] for card in context["disagreements"]}


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": " "},
        {"experiment_run_id": ""},
        {"experiment_run_id": "unfinished-run"},
    ],
)
def test_draft_does_not_bypass_human_reason_or_experiment_confirmation(
    prepared, monkeypatch, changes
):
    from research_app import experiment

    store, _, _ = prepared
    run = generated(prepared)
    monkeypatch.setattr(
        experiment,
        "verified_summary",
        lambda *args, **kwargs: {"status": "validation_complete"},
    )
    with pytest.raises(ValueError):
        save(store, run, [], final=False, **changes)
    assert list_decisions(store) == []


@pytest.mark.parametrize(
    "damage", ["deleted", "changed", "unsafe_hash", "symlink", "directory_symlink"]
)
def test_original_pdf_integrity_gates_all_formal_consumers(prepared, damage):
    store, source, cards = prepared
    run = generated(prepared)
    saved = save(store, run, [cards[0]["id"]])
    assert export.process_snapshot(store)["decisions"][0]["effective_status"] == "final"
    path = store.papers_dir / f"{source['sha256']}.pdf"
    original = path.read_bytes()
    if damage == "deleted":
        path.unlink()
    elif damage == "changed":
        before = path.stat()
        # 同长度、同mtime仍须重新计算哈希，不允许文件属性缓存掩盖替换。
        path.write_bytes(original[:-1] + (b"X" if original[-1:] != b"X" else b"Y"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif damage == "unsafe_hash":
        with store.connection() as db:
            db.execute(
                "UPDATE sources SET sha256='../outside' WHERE id=?", (source["id"],)
            )
    elif damage == "symlink":
        outside = store.data_dir / "outside.pdf"
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
    else:
        moved = store.data_dir / "moved-papers"
        store.papers_dir.rename(moved)
        store.papers_dir.symlink_to(moved, target_is_directory=True)
    assert not evidence.eligible_card(store, cards[0]["id"])
    snapshot = export.process_snapshot(store)
    assert snapshot["evidence"][0]["eligible"] is False
    assert snapshot["status"] == "incomplete"
    assert snapshot["decisions"][0]["status"] == "final"
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert any(
        "原PDF完整性" in reason for reason in snapshot["decisions"][0]["gate_reasons"]
    )
    assert list_decisions(store)[0] == saved
    assert get_card(store, cards[0]["id"])["review_status"] == "supported"
    backend = ControlledBackend(candidate_output([cards[0]["id"]]))
    with pytest.raises(ValueError, match="没有合格证据"):
        generate_candidates(store, backend, sms_case=True)
    assert backend.requests == []
    with pytest.raises(ValueError, match="原PDF完整性"):
        save(store, run, [cards[0]["id"]])
    with pytest.raises(export.ExportError, match="科研门禁"):
        export.import_case_pdf(
            store, store.data_dir / "unused.pdf", fonts_licensed=True
        )


def test_source_checks_are_shared_only_within_current_operation(prepared, monkeypatch):
    store, source, cards = prepared
    review_card(
        store, cards[1]["id"], "supported", "第二张卡同样已对照原PDF", pdf_opened=True
    )
    original_reader = documents.pdf_bytes
    reads = []

    def tracked_reader(current_store, source_id):
        reads.append(source_id)
        return original_reader(current_store, source_id)

    monkeypatch.setattr(documents, "pdf_bytes", tracked_reader)
    assert len(decision_context(store)["eligible_cards"]) == 2
    assert reads == [source["id"]]
    reads.clear()
    source_checks = {}
    assert evidence.eligible_card(store, cards[0]["id"], source_checks=source_checks)
    assert evidence.eligible_card(store, cards[1]["id"], source_checks=source_checks)
    export.process_snapshot(store, source_checks=source_checks)
    assert reads == [source["id"]]
    (store.papers_dir / f"{source['sha256']}.pdf").unlink()
    assert not decision_context(store)["eligible_cards"]
    assert reads == [source["id"], source["id"]]


@pytest.mark.parametrize("sms_marker", ["candidate", "question"])
def test_sms_final_requires_experiment_even_for_legacy_candidate_metadata(
    prepared, sms_marker
):
    store, _, cards = prepared
    run = generated(prepared)
    if sms_marker == "question":
        with store.connection() as db:
            brief = store.latest_brief()
            brief["content"]["question"] = SMS_QUESTION
            db.execute(
                "UPDATE briefs SET content=? WHERE revision=1",
                (json.dumps(brief["content"]),),
            )
            candidate = json.loads(
                db.execute(
                    "SELECT content FROM candidate_runs WHERE id=?", (run["id"],)
                ).fetchone()["content"]
            )
            candidate["sms_case"] = False
            db.execute(
                "UPDATE candidate_runs SET content=? WHERE id=?",
                (json.dumps(candidate), run["id"]),
            )
    with pytest.raises(ValueError, match="SMS正式决策必须绑定"):
        save(store, run, [cards[0]["id"]], experiment_run_id=None)
    assert list_decisions(store) == []
    draft = save(store, run, [cards[0]["id"]], experiment_run_id=None, final=False)
    assert draft["status"] == "draft"
    with store.connection() as db:
        db.execute("UPDATE decisions SET status='final' WHERE id=?", (draft["id"],))
    snapshot = export.process_snapshot(store)
    assert snapshot["decisions"][0]["status"] == "final"
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert any(
        "SMS正式决策必须绑定" in reason
        for reason in snapshot["decisions"][0]["gate_reasons"]
    )


def test_sqlite_backup_legacy_sms_final_without_binding_is_only_dynamically_demoted(
    prepared, tmp_path
):
    store, source, cards = prepared
    run = generated(prepared)
    saved = save(store, run, [cards[0]["id"]])
    # 模拟旧版本存下的final；通过SQLite backup迁入，不调用新保存接口补审。
    with store.connection() as db:
        db.execute(
            "UPDATE decisions SET experiment_run_id=NULL WHERE id=?", (saved["id"],)
        )
    imported = Store(tmp_path / "imported")
    with store.connection() as original, imported.connection() as target:
        original.backup(target)
    shutil.copytree(store.papers_dir, imported.papers_dir, dirs_exist_ok=True)
    historical = list_decisions(imported)
    snapshot = export.process_snapshot(imported)
    assert snapshot["evidence"][0]["eligible"] is True
    assert snapshot["decisions"][0]["status"] == "final"
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert any(
        "SMS正式决策必须绑定" in reason
        for reason in snapshot["decisions"][0]["gate_reasons"]
    )
    assert list_decisions(imported) == historical
    assert get_card(imported, cards[0]["id"])["review_status"] == "supported"


@pytest.mark.parametrize(
    "damage", ["wrong_candidate", "old_revision", "tampered_result"]
)
def test_existing_sms_final_rechecks_frozen_experiment_on_snapshot(prepared, damage):
    store, _, cards = prepared
    run = generated(prepared)
    saved = save(store, run, [cards[0]["id"]])
    path = store.data_dir / "experiments" / saved["experiment_run_id"]
    if damage == "wrong_candidate":
        other = next(name for name in SMS_METHODS if name != saved["chosen_candidate"])
        with store.connection() as db:
            db.execute(
                "UPDATE decisions SET chosen_candidate=? WHERE id=?",
                (other, saved["id"]),
            )
    elif damage == "old_revision":
        frozen_path = path / "frozen_selection.json"
        frozen = json.loads(frozen_path.read_text())
        frozen["study_revision"] = 2
        frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
        # 构造真实完整性通过、但属于另一研究版本的历史运行。
        experiment._seal(path)
    else:
        (path / "test.csv").write_text("damaged experiment", encoding="utf-8")
    snapshot = export.process_snapshot(store)
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert snapshot["decisions"][0]["gate_reasons"]
    with pytest.raises(ValueError):
        save(
            store,
            run,
            [cards[0]["id"]],
            chosen_candidate=other
            if damage == "wrong_candidate"
            else saved["chosen_candidate"],
        )
