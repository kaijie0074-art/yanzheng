"""受控后端与真实Store验证评价契约；不调用真实模型、不运行SMS训练。"""

import copy
import csv
import json
from io import BytesIO
from uuid import uuid4

import pytest
from pydantic import ValidationError
from reportlab.pdfgen.canvas import Canvas

from research_app import evaluation, experiment
from research_app.backends import BackendError
from research_app.decision import generate_candidates, save_decision
from research_app.documents import import_pdf
from research_app.evidence import analyze_pages, review_card
from research_app.models import BriefContent, ModelConfig, ModelReply, now
from research_app.storage import Store
from research_app.workflow import confirm_brief


class ControlledBackend:
    def __init__(
        self, output, *, model="test/unit", actual=None, fail=False, callback=None
    ):
        self.config = ModelConfig(local_model=model)
        self.output = output
        self.actual = actual or model
        self.fail = fail
        self.callback = callback
        self.requests = []
        self.text = json.dumps(output, ensure_ascii=False, indent=3) + "\n"

    def complete_json(self, **request):
        self.requests.append(copy.deepcopy(request))
        if self.callback:
            self.callback()
        if self.fail:
            raise BackendError("TIMEOUT", "测试超时")
        return ModelReply(
            value=request["schema"].model_validate(self.output),
            requested_model=self.config.local_model,
            actual_model=self.actual,
            backend="controlled_test",
            usage=None,
            called_at=now(),
            text=self.text,
        )


def answers(source_id):
    return {
        "answers": [
            {
                "question_id": f"q{index}",
                "answer": "仅按材料说明，不能外推。",
                "claims": [
                    {
                        "claim": f"主张{index}需要人工判断",
                        "source_id": source_id,
                        "page_number": 1,
                    }
                ],
            }
            for index in range(1, 7)
        ]
    }


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    brief = BriefContent(
        question="有限标签下英文SMS基线比较",
        population_or_object="公开历史英文SMS",
        constraints="有限标签",
        available_resources="本机CPU",
        validation_goal="spam F1与ham误报率",
    )
    confirm_brief(store, brief, ["SMS classification"])
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(
        50, 750, "Simple baselines require evaluation on the target corpus."
    )
    canvas.save()
    source = import_pdf(
        store,
        buffer.getvalue(),
        title="Synthetic test paper",
        authors=["Test Author"],
        year=2026,
        official_url="https://example.org/paper",
    )
    store.set_bibliography(source["id"], True)
    pairs = [(source["id"], 1)]
    evidence = {
        "cards": [
            {
                "claim": "需要目标语料验证",
                "source_id": source["id"],
                "page_number": 1,
                "quote": "Simple baselines require evaluation on the target corpus.",
                "conditions": "目标语料",
                "limitations": "非SMS性能保证",
                "ai_relation": "supports",
            }
        ]
    }
    card = analyze_pages(
        store, ControlledBackend(evidence), pairs, rights_confirmed=True
    )[0]
    review_card(
        store, card["id"], "supported", "人工对照确认目标语料验证边界", pdf_opened=True
    )
    run_id = str(uuid4())
    summary = {
        "run_id": run_id,
        "status": "confirmed",
        "recommended_candidate": "word_lr",
        "snapshot_sha256": "a" * 64,
        "frozen_selection": {"study_revision": 1, "chosen_candidate": "word_lr"},
        "test_fit_count": 5,
        "test_aggregates": [{"spam_f1_mean": 0.6}],
        "limitations": ["自造测试摘要，不代表真实实验"],
    }
    monkeypatch.setattr(
        evaluation, "verified_summary", lambda *args, **kwargs: copy.deepcopy(summary)
    )
    monkeypatch.setattr(
        experiment, "verified_summary", lambda *args, **kwargs: copy.deepcopy(summary)
    )
    candidate = {
        "candidates": [
            {
                "candidate_id": cid,
                "method": method,
                "evidence_ids": [card["id"]],
                "conditions": "仅目标语料",
                "feasibility": "本机CPU",
                "minimal_validation": "固定划分",
                "objections": "不能外推",
            }
            for cid, method in (
                ("word_nb", "TF-IDF + MultinomialNB"),
                ("word_lr", "TF-IDF + LogisticRegression"),
            )
        ]
    }
    run = generate_candidates(store, ControlledBackend(candidate), sms_case=True)
    decision = save_decision(
        store,
        candidate_run_id=run["id"],
        chosen_candidate="word_lr",
        reason="人工按验证结果选择",
        evidence_ids=[card["id"]],
        experiment_run_id=run_id,
        final=True,
    )
    case = store.data_dir / "case"
    case.mkdir()
    (case / "materials.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "prepared_at": now(),
                "dataset_page": "https://archive.ics.uci.edu/dataset/228/sms+spam+collection",
                "official_reported_sample_count": 5574,
                "dataset_license": {
                    "name": "CC BY 4.0",
                    "status": "official_page_statement_checked",
                    "page_sha256": "b" * 64,
                },
                "files": {
                    name: {"license": {"name": None, "status": "not_verified"}}
                    for name in ("P12-2018.pdf", "E17-2068.pdf")
                },
                "api_key": "sk-secret-material-should-not-export",
                "local_path": "/Users/private/input",
                "raw_sms": "SMS_BODY_MUST_NOT_LEAK",
                "account": "secret-account",
            }
        ),
        encoding="utf-8",
    )
    return {
        "store": store,
        "pairs": pairs,
        "run_id": run_id,
        "output_dir": tmp_path / "artifacts",
        "source": source,
        "card": card,
        "decision": decision,
        "brief": brief,
        "summary": summary,
    }


def direct(prepared, backend=None):
    backend = backend or ControlledBackend(answers(prepared["source"]["id"]))
    record = evaluation.start_direct(
        prepared["store"],
        backend,
        prepared["pairs"],
        experiment_run_id=prepared["run_id"],
        rights_confirmed=True,
        output_dir=prepared["output_dir"],
    )
    return record, backend


def workbench(prepared, record, backend=None):
    backend = backend or ControlledBackend(answers(prepared["source"]["id"]))
    result = evaluation.run_workbench(
        prepared["store"], backend, record["id"], output_dir=prepared["output_dir"]
    )
    return result, backend


def review(prepared, record, **changes):
    args = dict(
        method="direct",
        question_id="q1",
        claim_index=0,
        reviewer_id="R1",
        source_locatable=True,
        source_supports=False,
        within_scope=True,
        correction_count=0,
        note="人工核对：可定位，但原句不支持当前推论",
        review_seconds=2.5,
        output_dir=prepared["output_dir"],
    )
    args.update(changes)
    return evaluation.review_claim(prepared["store"], record["id"], **args)


def csv_rows(prepared):
    with (prepared["output_dir"] / "evaluation.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        return list(csv.DictReader(handle))


def test_same_full_packet_six_questions_and_original_model_text(prepared):
    record, first = direct(prepared)
    paired, second = workbench(prepared, record)
    assert len(first.requests) == len(second.requests) == 1
    left, right = first.requests[0]["payload"], second.requests[0]["payload"]
    assert left["packet"] == right["packet"] == paired["packet"]
    assert left["questions"] == right["questions"] == list(evaluation.QUESTIONS)
    assert first.requests[0]["system"] == second.requests[0]["system"]
    assert set(right) - set(left) == {"reviewed_cards", "decision_context"}
    assert right["reviewed_cards"][0]["id"] == prepared["card"]["id"]
    assert right["decision_context"]["id"] == prepared["decision"]["id"]
    assert (
        left["packet"]["pages"][0]["text"]
        == prepared["store"].get_pages(prepared["source"]["id"])[0]["text"]
    )
    assert left["packet"]["experiment_summary"] == prepared["summary"]
    directory = prepared["store"].data_dir / "evaluations" / record["id"]
    assert (directory / "direct_raw.json").read_text() == first.text
    assert (directory / "workbench_raw.json").read_text() == second.text
    assert paired["status"] == "pending"
    assert paired["pending_claim_count"] == 12
    assert all(row["review_status"] == "pending" for row in csv_rows(prepared))
    assert all(
        row["source_supports"]
        == row["correction_count"]
        == row["human_review_seconds"]
        == ""
        for row in csv_rows(prepared)
    )


def test_refresh_and_repeated_success_do_not_call_model(prepared):
    record, backend = direct(prepared)
    paired, second = workbench(prepared, record)
    reopened = Store(prepared["store"].data_dir)
    assert evaluation.get_evaluation(reopened, record["id"]) == paired
    assert evaluation.list_evaluations(reopened) == [paired]
    assert (
        evaluation.run_workbench(
            reopened, second, record["id"], output_dir=prepared["output_dir"]
        )
        == paired
    )
    assert len(backend.requests) == len(second.requests) == 1


def test_requested_model_mixing_rejected_before_call(prepared):
    record, _ = direct(prepared)
    backend = ControlledBackend(answers(prepared["source"]["id"]), model="test/other")
    with pytest.raises(ValueError, match="混用"):
        workbench(prepared, record, backend)
    assert backend.requests == []
    assert (
        evaluation.get_evaluation(prepared["store"], record["id"])["methods"]["direct"][
            "status"
        ]
        == "success"
    )


def test_actual_model_mixing_rejected_and_failure_keeps_direct(prepared):
    record, first = direct(prepared)
    backend = ControlledBackend(answers(prepared["source"]["id"]), actual="test/other")
    with pytest.raises(ValueError, match="模型"):
        workbench(prepared, record, backend)
    result = evaluation.get_evaluation(prepared["store"], record["id"])
    assert result["methods"]["workbench"]["status"] == "failed"
    assert (
        result["methods"]["direct"]["answers"] == record["methods"]["direct"]["answers"]
    )
    path = prepared["store"].data_dir / "evaluations" / record["id"] / "direct_raw.json"
    assert path.read_text() == first.text
    with pytest.raises(ValueError, match="已尝试"):
        workbench(prepared, record, backend)
    assert len(backend.requests) == 1


def test_backend_failure_preserves_direct_and_audit(prepared):
    record, _ = direct(prepared)
    with pytest.raises(BackendError):
        workbench(
            prepared,
            record,
            ControlledBackend(answers(prepared["source"]["id"]), fail=True),
        )
    saved = evaluation.get_evaluation(prepared["store"], record["id"])
    assert saved["methods"]["direct"]["status"] == "success"
    assert saved["methods"]["workbench"]["status"] == "failed"
    assert saved["methods"]["workbench"]["model_seconds"] >= 0
    assert (
        prepared["store"].rows(
            "SELECT status FROM model_runs WHERE task='evaluation_workbench'"
        )[0]["status"]
        == "failed"
    )


def test_direct_failure_remains_visible(prepared):
    with pytest.raises(BackendError):
        direct(
            prepared, ControlledBackend(answers(prepared["source"]["id"]), fail=True)
        )
    records = evaluation.list_evaluations(prepared["store"])
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert records[0]["methods"]["direct"]["status"] == "failed"
    assert records[0]["methods"]["workbench"]["status"] == "pending"


def test_revision_or_withdrawn_review_prevents_new_model_call(prepared):
    record, _ = direct(prepared)
    backend = ControlledBackend(answers(prepared["source"]["id"]))
    review_card(
        prepared["store"],
        prepared["card"]["id"],
        "unsupported",
        "人工撤销支持",
        pdf_opened=True,
    )
    with pytest.raises(ValueError, match="合格"):
        workbench(prepared, record, backend)
    changed = prepared["brief"].model_copy(update={"question": "新研究问题"})
    confirm_brief(prepared["store"], changed, ["new query"])
    with pytest.raises(ValueError, match="修订"):
        workbench(prepared, record, backend)
    assert backend.requests == []
    assert (
        evaluation.get_evaluation(prepared["store"], record["id"])["current_revision"]
        is False
    )


def test_independent_reviews_append_history_and_count_all_time(prepared, monkeypatch):
    ticks = iter((10.0, 12.0, 20.0, 23.0))
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: next(ticks))
    record, _ = direct(prepared)
    paired, _ = workbench(prepared, record)
    first = review(prepared, paired)
    second = review(
        prepared,
        first,
        reviewer_id="R2",
        source_supports=True,
        correction_count=1,
        review_seconds=1.5,
        note="独立复核后纠正范围判断",
    )
    assert len(second["reviews"]) == 2
    assert second["reviews"][0]["source_supports"] is False
    assert second["reviews"][1]["source_supports"] is True
    assert second["methods"]["direct"]["review_seconds"] == 4.0
    assert second["methods"]["direct"]["total_seconds"] == 6.0
    assert second["status"] == "pending"
    for method in evaluation.METHODS:
        for number in range(1, 7):
            if method == "direct" and number == 1:
                continue
            second = review(
                prepared,
                second,
                method=method,
                question_id=f"q{number}",
                reviewer_id="R2",
                review_seconds=1,
            )
    assert second["status"] == "complete"
    assert second["pending_claim_count"] == 0
    # 不支持的结果同样是完成的人审，不以高分作为完成条件。
    assert any(row["source_supports"] == "False" for row in csv_rows(prepared))
    assert second["methods"]["direct"]["total_seconds"] == 11.0
    assert second["methods"]["workbench"]["total_seconds"] == 9.0


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewer_id": "AI"},
        {"reviewer_id": "R0"},
        {"reviewer_id": "张三"},
        {"reviewer_id": "R1\n"},
        {"reviewer_id": None},
        {"note": " "},
        {"note": None},
        {"source_locatable": 1},
        {"source_supports": "true"},
        {"within_scope": None},
        {"review_seconds": -1},
        {"review_seconds": float("nan")},
        {"review_seconds": float("inf")},
        {"review_seconds": True},
        {"review_seconds": "1"},
        {"correction_count": -1},
        {"correction_count": True},
        {"correction_count": 1.5},
        {"method": "AI"},
        {"question_id": "q7"},
        {"claim_index": -1},
        {"claim_index": True},
        {"claim_index": 3},
        {"note": "api_key=sk-not-for-export"},
        {"note": "/Users/private/account"},
    ],
)
def test_invalid_review_inputs_never_create_reviews(prepared, changes):
    record, _ = direct(prepared)
    with pytest.raises(ValueError):
        review(prepared, record, **changes)
    assert evaluation.get_evaluation(prepared["store"], record["id"])["reviews"] == []


def test_csv_and_material_packet_exclude_secrets_and_sms(prepared, monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-env-secret-never-read")
    record, _ = direct(prepared)
    review(prepared, record)
    csv_text = (prepared["output_dir"] / "evaluation.csv").read_text()
    packet_text = json.dumps(record["packet"])
    for forbidden in (
        "sk-env-secret",
        "sk-secret-material",
        "/Users/",
        "SMS_BODY_MUST_NOT_LEAK",
        "secret-account",
    ):
        assert forbidden not in csv_text
        assert forbidden not in packet_text
    assert "Simple baselines" not in csv_text
    assert "CC BY 4.0" in packet_text


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "four_claims",
        "empty_claims",
        "omitted_null",
        "extra_score",
    ],
)
def test_schema_requires_all_six_questions_and_no_model_review(mutation):
    output = answers(None)
    if mutation == "missing":
        output["answers"].pop()
    elif mutation == "duplicate":
        output["answers"][-1]["question_id"] = "q1"
    elif mutation == "four_claims":
        output["answers"][0]["claims"] *= 4
    elif mutation == "empty_claims":
        output["answers"][0]["claims"] = []
    elif mutation == "omitted_null":
        del output["answers"][0]["claims"][0]["source_id"]
    else:
        output["answers"][0]["claims"][0]["source_supports"] = True
    with pytest.raises(ValidationError):
        evaluation.EvaluationOutput.model_validate(output)


def test_rights_and_experiment_requirements(prepared):
    backend = ControlledBackend(answers(prepared["source"]["id"]))
    with pytest.raises(ValueError, match="确认有权"):
        evaluation.start_direct(
            prepared["store"],
            backend,
            prepared["pairs"],
            experiment_run_id=prepared["run_id"],
            rights_confirmed=False,
            output_dir=prepared["output_dir"],
        )
    prepared["summary"]["status"] = "validation_complete"
    with pytest.raises(ValueError, match="成功实验"):
        direct(prepared, backend)
    assert backend.requests == []


def test_paths_and_packet_tampering_rejected(prepared):
    record, _ = direct(prepared)
    with pytest.raises(ValueError, match="UUID"):
        evaluation.get_evaluation(prepared["store"], "../../other")
    path = prepared["store"].data_dir / "evaluations" / record["id"] / "packet.json"
    packet = json.loads(path.read_text())
    packet["pages"][0]["text"] = "篡改原文"
    path.write_text(json.dumps(packet))
    backend = ControlledBackend(answers(prepared["source"]["id"]))
    with pytest.raises(ValueError, match="修改|完整性"):
        workbench(prepared, record, backend)
    assert backend.requests == []


def test_csv_formula_is_not_executable(prepared):
    output = answers(prepared["source"]["id"])
    output["answers"][0]["claims"][0]["claim"] = '=HYPERLINK("https://example.org")'
    record, backend = direct(prepared, ControlledBackend(output))
    assert csv_rows(prepared)[0]["claim"].startswith("'=")
    raw = prepared["store"].data_dir / "evaluations" / record["id"] / "direct_raw.json"
    assert raw.read_text() == backend.text


def test_paper_heading_colon_newline_is_not_a_windows_path(prepared):
    store = prepared["store"]
    original = "Methods:\nA linear model:\nResults:\nSimple baselines require evaluation on the target corpus.\n"
    with store.connection() as db:
        db.execute(
            "UPDATE pages SET text=? WHERE source_id=? AND page_number=1",
            (original, prepared["source"]["id"]),
        )
    output = answers(prepared["source"]["id"])
    output["answers"][0]["answer"] = "License:\nCC BY 4.0 applies to the dataset."
    record, _ = direct(prepared, ControlledBackend(output))
    assert record["methods"]["direct"]["status"] == "success"
    assert record["packet"]["pages"][0]["text"] == original
    assert (
        record["methods"]["direct"]["answers"][0]["answer"]
        == output["answers"][0]["answer"]
    )


@pytest.mark.parametrize(
    "local_path", ["/Users/private/research", r"C:\Users\private\research"]
)
def test_real_local_paths_in_paper_packet_are_rejected(prepared, local_path):
    store = prepared["store"]
    with store.connection() as db:
        db.execute(
            "UPDATE pages SET text=? WHERE source_id=?",
            ("Private source " + local_path, prepared["source"]["id"]),
        )
    backend = ControlledBackend(answers(prepared["source"]["id"]))
    with pytest.raises(ValueError, match="匿名处理"):
        direct(prepared, backend)
    assert backend.requests == []
