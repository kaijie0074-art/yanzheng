from __future__ import annotations

import json
import sqlite3
from io import BytesIO

import pytest
from pydantic import BaseModel, ValidationError
from reportlab.pdfgen import canvas

from research_app import evidence
from research_app.backends import BackendError
from research_app.documents import import_pdf
from research_app.models import EvidenceOutput, ModelReply, now
from research_app.storage import Store, encoded, uid
from research_app.workflow import confirm_brief


class FakeBackend:
    def __init__(self, cards, *, unchecked=False, error=None):
        self.cards = cards
        self.unchecked = unchecked
        self.error = error
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        data = {"cards": self.cards}
        if self.unchecked:

            class Unchecked(BaseModel):
                cards: list[dict]

            value = Unchecked(**data)
        else:
            value = kwargs["schema"].model_validate(data)
        return ModelReply(
            value=value,
            requested_model="test/model",
            actual_model="test/model",
            backend="test",
            usage=None,
            called_at=now(),
            text=encoded(data),
        )


def brief(store, question="Compare classification methods"):
    return confirm_brief(
        store,
        {
            "question": question,
            "population_or_object": "English text",
            "constraints": "Limited labels",
            "available_resources": "Local CPU",
            "validation_goal": "Compare held-out F1",
        },
        ["text classification baseline"],
    )


def paper(store, *texts):
    output = BytesIO()
    pdf = canvas.Canvas(output, invariant=1)
    for text in texts:
        pdf.drawString(30, 750, text)
        pdf.showPage()
    pdf.save()
    source = import_pdf(
        store,
        output.getvalue(),
        title="Synthetic paper",
        authors=["Test Author"],
        year=2025,
        official_url="https://example.test/paper",
    )
    store.set_bibliography(source["id"], True)
    return source


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data")
    brief(store)
    source = paper(
        store,
        "A simple baseline works on this dataset.",
        "Only English text was studied.",
    )
    return store, source


def content(source, **changes):
    return {
        "claim": "A simple baseline may be worth testing.",
        "source_id": source["id"],
        "page_number": 1,
        "quote": "A simple baseline works on this dataset.",
        "conditions": "This dataset only",
        "limitations": "No SMS result",
        "ai_relation": "supports",
        **changes,
    }


def analyze(store, source, cards=None, *, pairs=None):
    backend = FakeBackend([content(source)] if cards is None else cards)
    result = evidence.analyze_pages(
        store,
        backend,
        [(source["id"], 1)] if pairs is None else pairs,
        rights_confirmed=True,
    )
    return result, backend


def supported(store, source):
    card = analyze(store, source)[0][0]
    return evidence.review_card(
        store, card["id"], "supported", "Opened PDF; claim is bounded.", pdf_opened=True
    )


def formal_decision(store, card):
    decision_id = uid()
    with store.connection() as db:
        db.execute(
            "INSERT INTO decisions(id,study_revision,chosen_candidate,reason,evidence_ids,experiment_run_id,status,created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                card["study_revision"],
                "word_nb",
                "Human choice",
                encoded([card["id"]]),
                None,
                "final",
                now(),
                None,
            ),
        )
    return decision_id


def assert_invalidated(store, decision_id):
    decision = store.rows("SELECT * FROM decisions WHERE id=?", (decision_id,))[0]
    assert decision["status"] == "draft"
    assert decision["invalidated_at"]


def test_valid_quote_is_unreviewed_and_payload_contains_only_selected_pages(workspace):
    store, source = workspace
    cards, backend = analyze(store, source)
    card = cards[0]
    assert card["quote_match"] is True
    assert card["review_status"] == "unreviewed"
    assert card["reviewed_at"] is None
    assert not evidence.eligible_card(store, card["id"])
    assert backend.calls[0]["schema"] is EvidenceOutput
    assert backend.calls[0]["payload"]["pages"] == [store.get_pages(source["id"])[0]]
    run = store.rows("SELECT * FROM model_runs")[0]
    assert card["analysis_id"] == run["id"]
    assert run["study_revision"] == card["study_revision"] == 1
    assert json.loads(run["input_refs"])["pages"] == [
        {"source_id": source["id"], "page_number": 1}
    ]
    assert run["status"] == "success"
    reopened = Store(store.data_dir)
    assert evidence.get_card(reopened, card["id"]) == card
    assert evidence.list_cards(reopened, revision=1) == cards
    assert evidence.list_cards(reopened, revision=2) == []
    assert not evidence.eligible_card(store, "missing")


def test_true_quote_does_not_force_supported_inference(workspace):
    store, source = workspace
    card = analyze(
        store,
        source,
        [content(source, claim="All languages always reach perfect accuracy.")],
    )[0][0]
    reviewed = evidence.review_card(
        store,
        card["id"],
        "unsupported",
        "Quote does not support universal accuracy.",
        pdf_opened=True,
    )
    assert reviewed["quote_match"] is True
    assert reviewed["review_status"] == "unsupported"
    assert not evidence.eligible_card(store, card["id"])


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"source_id": "not-in-input"}, "来源"),
        ({"page_number": 99}, "物理页码"),
        ({"page_number": 2, "quote": "Only English text was studied."}, "物理页码"),
        ({"quote": "A simple baseline always works."}, "引文"),
        ({"quote": "a simple baseline works on this dataset."}, "引文"),
        ({"quote": "A simple baseline works on this dataset!"}, "引文"),
        ({"quote": "  \n\t"}, "引文"),
    ],
)
def test_invalid_cards_are_retained_but_cannot_be_supported(workspace, changes, reason):
    store, source = workspace
    card = analyze(store, source, [content(source, **changes)])[0][0]
    assert card["quote_match"] is False
    assert reason in card["validation_note"]
    with pytest.raises(ValueError, match="不能标为supported"):
        evidence.review_card(
            store, card["id"], "supported", "Model says so", pdf_opened=True
        )
    assert evidence.get_card(store, card["id"])["review_status"] == "unreviewed"
    assert not evidence.eligible_card(store, card["id"])


def test_existing_unselected_source_is_not_accepted(workspace):
    store, source = workspace
    other = paper(store, "Other paper exists.")
    card = analyze(store, source, [content(other, quote="Other paper exists.")])[0][0]
    assert not card["quote_match"]
    assert "来源" in card["validation_note"]


def test_only_nfkc_and_whitespace_normalization_allowed(workspace):
    store, source = workspace
    quote = "Ａ simple\n\tbaseline   works on this dataset."
    card = analyze(store, source, [content(source, quote=quote)])[0][0]
    assert card["quote_match"]
    assert card["content"]["quote"] == quote
    assert card["review_status"] == "unreviewed"


def test_abstract_cannot_invent_pdf_pages(workspace):
    store, source = workspace
    abstract_id = store.add_source(
        title="Abstract only",
        authors=["Author"],
        year=2025,
        official_url="https://example.test/abstract",
        access_level="abstract",
    )
    store.set_bibliography(abstract_id, True)
    card = analyze(store, source, [content(source, source_id=abstract_id)])[0][0]
    assert not card["quote_match"]
    with pytest.raises(ValueError):
        evidence.review_card(
            store, card["id"], "supported", "Invented page", pdf_opened=True
        )
    backend = FakeBackend([])
    with pytest.raises(ValueError, match="物理页码不存在"):
        evidence.analyze_pages(
            store, backend, [(abstract_id, 1)], rights_confirmed=True
        )
    assert not backend.calls


@pytest.mark.parametrize(
    "status,note,opened",
    [
        ("supported", "", True),
        ("supported", " \t", True),
        ("supported", "Reason", False),
        ("unsupported", "Reason", False),
        ("model_verified", "Reason", True),
    ],
)
def test_review_requires_human_inputs(workspace, status, note, opened):
    store, source = workspace
    card = analyze(store, source)[0][0]
    with pytest.raises(ValueError):
        evidence.review_card(store, card["id"], status, note, pdf_opened=opened)
    assert evidence.get_card(store, card["id"]) == card
    assert store.rows("SELECT * FROM evidence_reviews") == []


def test_bibliography_must_be_confirmed_before_supported(workspace):
    store, source = workspace
    store.set_bibliography(source["id"], False)
    card = analyze(store, source)[0][0]
    assert card["quote_match"]
    with pytest.raises(ValueError, match="书目"):
        evidence.review_card(
            store, card["id"], "supported", "Read PDF", pdf_opened=True
        )
    store.set_bibliography(source["id"], True)
    reviewed = evidence.review_card(
        store,
        card["id"],
        "supported",
        "Read PDF and confirmed metadata",
        pdf_opened=True,
    )
    assert reviewed["reviewed_at"]
    assert evidence.eligible_card(store, card["id"], revision=1)
    assert not evidence.eligible_card(store, card["id"], revision=2)


def test_bibliography_revocation_resets_review_and_final_decision(workspace):
    store, source = workspace
    card = supported(store, source)
    decision_id = formal_decision(store, card)
    store.set_bibliography(source["id"], False)
    assert evidence.get_card(store, card["id"])["review_status"] == "unreviewed"
    assert not evidence.eligible_card(store, card["id"])
    assert_invalidated(store, decision_id)
    history = store.rows(
        "SELECT * FROM evidence_reviews WHERE card_id=?", (card["id"],)
    )
    assert history[-1]["previous_status"] == "supported"
    assert json.loads(history[-1]["previous_content"]) == card["content"]
    store.set_bibliography(source["id"], True)
    assert not evidence.eligible_card(store, card["id"])


@pytest.mark.parametrize(
    "changes",
    [
        {"claim": "A revised bounded claim."},
        {"quote": "A simple baseline"},
        {"source_id": "unknown"},
        {"page_number": 2},
        {"conditions": "Different conditions"},
        {"limitations": "More limitations"},
        {"ai_relation": "contradicts"},
    ],
)
def test_all_content_edits_reset_review_preserve_history_and_invalidate_final(
    workspace, changes
):
    store, source = workspace
    card = supported(store, source)
    decision_id = formal_decision(store, card)
    edited = evidence.edit_card(store, card["id"], {**card["content"], **changes})
    assert edited["review_status"] == "unreviewed"
    assert edited["review_note"] == ""
    assert edited["reviewed_at"] is None
    assert edited["analysis_id"] == card["analysis_id"]
    assert not evidence.eligible_card(store, card["id"])
    assert_invalidated(store, decision_id)
    history = store.rows(
        "SELECT * FROM evidence_reviews WHERE card_id=?", (card["id"],)
    )[-1]
    assert json.loads(history["previous_content"]) == card["content"]
    assert history["previous_status"] == "supported"
    assert (
        json.loads(history["review_note"])["previous_review_note"]
        == card["review_note"]
    )


def test_noop_edit_keeps_review_and_invalid_schema_is_atomic(workspace):
    store, source = workspace
    card = supported(store, source)
    assert evidence.edit_card(store, card["id"], card["content"]) == card
    count = len(store.rows("SELECT * FROM evidence_reviews"))
    with pytest.raises(ValidationError):
        evidence.edit_card(
            store, card["id"], {**card["content"], "review_status": "supported"}
        )
    assert evidence.get_card(store, card["id"]) == card
    assert len(store.rows("SELECT * FROM evidence_reviews")) == count


@pytest.mark.parametrize(
    "status", ["unreviewed", "partially_supported", "unsupported", "insufficient"]
)
def test_review_downgrade_invalidates_final(workspace, status):
    store, source = workspace
    card = supported(store, source)
    decision_id = formal_decision(store, card)
    evidence.review_card(
        store,
        card["id"],
        status,
        "Further inspection changes judgment",
        pdf_opened=True,
    )
    assert not evidence.eligible_card(store, card["id"])
    assert_invalidated(store, decision_id)


def test_question_revision_makes_old_cards_ineligible_but_preserves_history(workspace):
    store, source = workspace
    card = supported(store, source)
    decision_id = formal_decision(store, card)
    brief(store, "Different population and question")
    assert not evidence.eligible_card(store, card["id"])
    assert not evidence.eligible_card(store, card["id"], revision=1)
    assert evidence.list_cards(store, revision=1) == [card]
    assert_invalidated(store, decision_id)
    with pytest.raises(ValueError, match="当前研究修订"):
        evidence.review_card(
            store, card["id"], "supported", "Old context", pdf_opened=True
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE pages SET text='Replaced text' WHERE page_number=1",
        "UPDATE sources SET access_level='abstract'",
        "UPDATE sources SET bibliography_status='unverified'",
        "UPDATE sources SET page_count=0",
    ],
)
def test_eligibility_revalidates_live_source_and_text_not_cached_match(
    workspace, mutation
):
    store, source = workspace
    card = supported(store, source)
    with store.connection() as db:
        db.execute(mutation)
    assert evidence.get_card(store, card["id"])["quote_match"]
    assert not evidence.eligible_card(store, card["id"])


@pytest.mark.parametrize("rights", [False, None, 1, "true"])
def test_rights_gate_never_calls_model(workspace, rights):
    store, source = workspace
    backend = FakeBackend([])
    with pytest.raises(ValueError, match="有权"):
        evidence.analyze_pages(
            store, backend, [(source["id"], 1)], rights_confirmed=rights
        )
    assert not backend.calls
    assert store.rows("SELECT * FROM model_runs") == []


def test_confirmed_brief_required(tmp_path):
    store = Store(tmp_path / "empty")
    source = paper(store, "Some text")
    backend = FakeBackend([])
    with pytest.raises(ValueError, match="确认研究问题"):
        evidence.analyze_pages(
            store, backend, [(source["id"], 1)], rights_confirmed=True
        )
    assert not backend.calls


def test_character_limit_has_no_truncation_or_model_call(workspace):
    store, source = workspace
    with store.connection() as db:
        db.execute(
            "UPDATE pages SET text=? WHERE source_id=? AND page_number=1",
            ("x" * 24000, source["id"]),
        )
    cards, backend = analyze(store, source, [])
    assert cards == []
    assert len(backend.calls[0]["payload"]["pages"][0]["text"]) == 24000
    too_large = FakeBackend([])
    with pytest.raises(ValueError, match="24000"):
        evidence.analyze_pages(
            store,
            too_large,
            [(source["id"], 1), (source["id"], 2)],
            rights_confirmed=True,
        )
    assert not too_large.calls


@pytest.mark.parametrize("unchecked", [False, True])
def test_schema_failure_rejects_entire_batch_and_audits(workspace, unchecked):
    store, source = workspace
    backend = FakeBackend(
        [content(source), content(source, review_status="supported")],
        unchecked=unchecked,
    )
    with pytest.raises(ValidationError):
        evidence.analyze_pages(
            store, backend, [(source["id"], 1)], rights_confirmed=True
        )
    assert evidence.list_cards(store) == []
    assert len(store.get_pages(source["id"])) == 2
    runs = store.rows("SELECT * FROM model_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


def test_more_than_five_cards_rejected_atomically(workspace):
    store, source = workspace
    with pytest.raises(ValidationError):
        analyze(store, source, [content(source)] * 6)
    assert evidence.list_cards(store) == []


def test_write_failure_rolls_back_all_cards(workspace):
    store, source = workspace
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER reject_second BEFORE INSERT ON evidence_cards "
            "WHEN json_extract(NEW.content,'$.claim')='Reject insertion' "
            "BEGIN SELECT RAISE(ABORT,'test rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        analyze(
            store, source, [content(source), content(source, claim="Reject insertion")]
        )
    assert evidence.list_cards(store) == []
    assert len(store.get_pages(source["id"])) == 2


def test_backend_failure_preserves_material_and_audit(workspace):
    store, source = workspace
    backend = FakeBackend([], error=BackendError("TEST_FAILURE", "Synthetic failure"))
    with pytest.raises(BackendError):
        evidence.analyze_pages(
            store, backend, [(source["id"], 1)], rights_confirmed=True
        )
    assert evidence.list_cards(store) == []
    assert len(store.get_pages(source["id"])) == 2
    assert store.rows("SELECT status,error_type FROM model_runs") == [
        {"status": "failed", "error_type": "TEST_FAILURE"}
    ]


def test_selected_page_from_downgraded_abstract_still_cannot_support(workspace):
    store, source = workspace
    with store.connection() as db:
        db.execute(
            "UPDATE sources SET access_level='abstract' WHERE id=?", (source["id"],)
        )
    card = analyze(store, source)[0][0]
    assert not card["quote_match"]
    assert "摘要" in card["validation_note"]
    with pytest.raises(ValueError, match="不能标为supported"):
        evidence.review_card(
            store,
            card["id"],
            "supported",
            "Page row alone is not fulltext",
            pdf_opened=True,
        )


def test_corrected_quote_requires_new_human_review(workspace):
    store, source = workspace
    card = analyze(store, source, [content(source, quote="A false invented quote.")])[
        0
    ][0]
    evidence.review_card(
        store, card["id"], "unsupported", "Quote is absent", pdf_opened=True
    )
    edited = evidence.edit_card(store, card["id"], content(source))
    assert edited["quote_match"]
    assert edited["review_status"] == "unreviewed"
    assert not evidence.eligible_card(store, card["id"])
    evidence.review_card(
        store,
        card["id"],
        "supported",
        "Reopened PDF; corrected quote and claim verified",
        pdf_opened=True,
    )
    assert evidence.eligible_card(store, card["id"])


def test_metadata_change_and_review_revocation_are_atomic(workspace):
    import sqlite3

    store, source = workspace
    card = supported(store, source)
    decision_id = formal_decision(store, card)
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_review_history BEFORE INSERT ON evidence_reviews BEGIN SELECT RAISE(ABORT,'audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.update_source_metadata(
            source["id"],
            title="Changed title",
            authors=source["authors"],
            year=source["year"],
            official_url=source["official_url"],
            doi=source["doi"],
        )
    unchanged = store.get_source(source["id"])
    assert unchanged["title"] == source["title"]
    assert unchanged["bibliography_status"] == "confirmed"
    assert evidence.eligible_card(store, card["id"])
    assert (
        store.rows("SELECT status FROM decisions WHERE id=?", (decision_id,))[0][
            "status"
        ]
        == "final"
    )
    with store.connection() as db:
        db.execute("DROP TRIGGER fail_review_history")
    store.update_source_metadata(
        source["id"],
        title="Changed title",
        authors=source["authors"],
        year=source["year"],
        official_url=source["official_url"],
        doi=source["doi"],
    )
    changed = store.get_source(source["id"])
    assert (
        changed["title"] == "Changed title"
        and changed["bibliography_status"] == "unverified"
    )
    assert evidence.get_card(store, card["id"])["review_status"] == "unreviewed"
    assert_invalidated(store, decision_id)
