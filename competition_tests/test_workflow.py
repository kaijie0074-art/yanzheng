import json

import pytest

from research_app.backends import BackendError
from research_app.models import BriefContent, ModelConfig
from research_app.storage import Store, encoded, now, uid
from research_app.workflow import clarify, confirm_brief


def brief(question="比较短信分类基线"):
    return BriefContent(
        question=question,
        population_or_object="历史英文SMS",
        constraints="有限训练标签",
        available_resources="本机CPU",
        validation_goal="spam F1与ham误报率",
    )


class FailingBackend:
    config = ModelConfig()
    calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        raise BackendError("SCHEMA_VALIDATION", "输出结构错误")


def test_empty_conditions_never_call_or_persist_model(tmp_path):
    store = Store(tmp_path)
    backend = FailingBackend()
    for interest, resources in [("", "CPU"), ("基线", "  ")]:
        with pytest.raises(ValueError):
            clarify(store, backend, interest, resources)
    assert backend.calls == 0
    assert store.rows("SELECT * FROM model_runs") == []


def test_bad_model_preserves_sources_and_audits_failure(tmp_path):
    store = Store(tmp_path)
    sid = store.add_source(
        title="来源",
        authors=["作者"],
        year=2020,
        official_url="https://example.org/paper",
    )
    with pytest.raises(BackendError):
        clarify(store, FailingBackend(), "文本分类", "本地计算")
    assert Store(tmp_path).get_source(sid)["title"] == "来源"
    run = store.rows("SELECT * FROM model_runs")[0]
    assert run["status"] == "failed" and run["error_type"] == "SCHEMA_VALIDATION"
    assert store.latest_brief() is None


def test_changed_question_invalidates_final_not_history(tmp_path):
    store = Store(tmp_path)
    one = confirm_brief(store, brief(), ["SMS text classification"])
    did = uid()
    with store.connection() as db:
        db.execute(
            "INSERT INTO decisions(id,study_revision,chosen_candidate,reason,evidence_ids,experiment_run_id,status,created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, 1, "word_nb", "人工理由", encoded([]), None, "final", now(), None),
        )
    assert confirm_brief(store, brief(), ["SMS text classification"])["revision"] == 1
    assert store.rows("SELECT status FROM decisions")[0]["status"] == "final"
    two = confirm_brief(store, brief("换一个研究问题"), ["different query"])
    assert two["revision"] == 2
    historical = store.rows("SELECT * FROM briefs ORDER BY revision")
    assert (
        json.loads(historical[0]["content"])["question"] == one["content"]["question"]
    )
    decision = store.rows("SELECT * FROM decisions WHERE id=?", (did,))[0]
    assert decision["status"] == "draft" and decision["invalidated_at"]
    assert Store(tmp_path).latest_brief()["revision"] == 2


def test_missing_research_conditions_cannot_confirm(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        confirm_brief(store, brief().model_copy(update={"constraints": ""}), ["query"])
    assert store.latest_brief() is None
