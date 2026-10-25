"""离线合成记录验证导出边界；不制作参赛成果，不调用模型或训练。"""

import csv
import json
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas

from research_app import decision, evidence, export
from research_app.documents import import_pdf
from research_app.models import ModelReply, now
from research_app.storage import Store, encoded
from research_app.workflow import confirm_brief


class ControlledBackend:
    def __init__(self, content):
        self.content = content

    def complete_json(self, **kwargs):
        return ModelReply(
            value=kwargs["schema"].model_validate(self.content),
            requested_model="test/model",
            actual_model="test/model",
            backend="controlled",
            usage=None,
            called_at=now(),
            text="RAW_MODEL_OUTPUT_DO_NOT_EXPORT api_key=never-export-this",
        )


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data")
    confirm_brief(
        store,
        {
            "question": "有限标签下比较分类基线",
            "population_or_object": "英文短信",
            "constraints": "有限标签",
            "available_resources": "本机CPU",
            "validation_goal": "保留集spam F1",
        },
        ["text classification"],
    )
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(
        30, 700, "Simple baselines require evaluation on the target dataset."
    )
    canvas.drawString(30, 670, "FULL_PAGE_ONLY_NOT_EXPORTED")
    canvas.save()
    source = import_pdf(
        store,
        buffer.getvalue(),
        title="Synthetic paper",
        authors=["Public Author"],
        year=2026,
        official_url="https://example.org/paper",
    )
    store.set_bibliography(source["id"], True)
    card = evidence.analyze_pages(
        store,
        ControlledBackend(
            {
                "cards": [
                    {
                        "claim": "简单基线需要目标语料验证",
                        "source_id": source["id"],
                        "page_number": 1,
                        "quote": "Simple baselines require evaluation on the target dataset.",
                        "conditions": "仅目标语料",
                        "limitations": "没有SMS性能保证",
                        "ai_relation": "supports",
                    }
                ]
            }
        ),
        [(source["id"], 1)],
        rights_confirmed=True,
    )[0]
    return store, source, card


def supported_decision(workspace, attach_confirmed_sms):
    store, _, card = workspace
    evidence.review_card(
        store, card["id"], "supported", "已打开原PDF，推论不越界", pdf_opened=True
    )
    summary = attach_confirmed_sms(store)
    output = {
        "candidates": [
            {
                "candidate_id": name,
                "method": method,
                "evidence_ids": [card["id"]],
                "conditions": "目标语料",
                "feasibility": "可运行",
                "minimal_validation": "验证集比较",
                "objections": "不能外推",
            }
            for name, method in decision.SMS_METHODS.items()
        ]
    }
    run = decision.generate_candidates(store, ControlledBackend(output), sms_case=True)
    return decision.save_decision(
        store,
        candidate_run_id=run["id"],
        chosen_candidate=summary["recommended_candidate"],
        reason="人工选择并绑定合成回归实验",
        evidence_ids=[card["id"]],
        experiment_run_id=summary["run_id"],
        final=True,
    )


@pytest.fixture
def preview_fonts():
    if not all(
        Path(path).is_file() for path, _ in (export.PREVIEW_SONG, export.PREVIEW_HEI)
    ):
        pytest.skip("本平台没有已知合法可嵌入的系统预览字体")


def test_empty_workspace_cannot_claim_completion(tmp_path):
    snapshot = export.process_snapshot(Store(tmp_path / "data"))
    paragraphs, notes, blocked = export._case_content(snapshot)
    assert snapshot["status"] == "incomplete"
    assert "研究问题尚未人工确认" in blocked
    assert "待人工" in "".join(paragraphs)
    assert "实验未完成确认" in "".join(paragraphs)
    assert export.character_count("".join(paragraphs)) <= 900
    assert (
        export.character_count(
            export.TITLE + "".join(paragraphs + notes) + export.PREVIEW
        )
        <= 1000
    )


def test_process_whitelist_and_unreviewed(workspace):
    store, source, card = workspace
    snapshot = export.process_snapshot(store)
    text = json.dumps(snapshot, ensure_ascii=False)
    assert "RAW_MODEL_OUTPUT" not in text
    assert "never-export-this" not in text
    assert "FULL_PAGE_ONLY_NOT_EXPORTED" not in text
    assert str(store.data_dir) not in text
    assert "Public Author" in text
    assert snapshot["evidence"][0]["quote_match"] is True
    assert snapshot["evidence"][0]["review_status"] == "unreviewed"
    assert snapshot["evidence"][0]["eligible"] is False
    assert snapshot["objections"] == [card["id"]]
    assert snapshot["status"] == "incomplete"
    assert snapshot["model_runs"][0]["actual_model"] == "test/model"
    assert snapshot["sources"][0]["official_url"] == "https://example.org/paper"
    assert snapshot["privacy"]["redacted_fields"] == []


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijk12345",
        "Bearer confidential-token",
        "api_key=secret-value",
        "/Users/private-person/file.txt",
        "/home/person/secrets.txt",
        "/tmp/upload.pdf",
        r"C:\Users\person\file.txt",
        "C:/Users/person/file.txt",
        "someone@example.com",
        "学校：隐私学校",
        "https://user:password@example.com/x",
    ],
)
def test_sensitive_whitelisted_fields_are_removed(workspace, secret):
    store, _, card = workspace
    evidence.review_card(store, card["id"], "insufficient", secret, pdf_opened=True)
    snapshot = export.process_snapshot(store)
    assert secret not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["privacy"]["redacted_fields"]
    assert "[敏感字段已移除" in snapshot["evidence"][0]["review_note"]


def test_known_sms_cannot_escape_through_human_note(workspace):
    store, _, card = workspace
    corpus = store.data_dir / "case"
    corpus.mkdir()
    message = "SYNTHETIC_SMS_DO_NOT_EXPORT_123456"
    (corpus / "SMSSpamCollection").write_text(
        "ham\t" + message + "\n", encoding="utf-8"
    )
    evidence.review_card(
        store, card["id"], "insufficient", "误贴短信：" + message, pdf_opened=True
    )
    assert message not in json.dumps(export.process_snapshot(store))


def test_formal_decision_is_dynamically_demoted_on_raw_page_change(
    workspace, attach_confirmed_sms
):
    store, source, _ = workspace
    record = supported_decision(workspace, attach_confirmed_sms)
    initial = export.process_snapshot(store)
    assert initial["decisions"][0]["effective_status"] == "final"
    # 绕过正常编辑接口模拟底层资料损坏；导出不得信任旧quote_match或final。
    with store.connection() as db:
        db.execute(
            "UPDATE pages SET text='Changed source page' WHERE source_id=?",
            (source["id"],),
        )
    snapshot = export.process_snapshot(store)
    assert snapshot["evidence"][0]["quote_match"] is False
    assert snapshot["decisions"][0]["status"] == "final"
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert snapshot["decisions"][0]["gate_reasons"]
    assert (
        store.rows("SELECT status FROM decisions WHERE id=?", (record["id"],))[0][
            "status"
        ]
        == "final"
    )


def test_metadata_revocation_preserves_review_history(workspace, attach_confirmed_sms):
    store, source, _ = workspace
    supported_decision(workspace, attach_confirmed_sms)
    store.set_bibliography(source["id"], False)
    snapshot = export.process_snapshot(store)
    assert snapshot["human_review_history"]
    assert snapshot["decisions"][0]["effective_status"] == "draft"
    assert snapshot["evidence"][0]["review_status"] == "unreviewed"


@pytest.mark.parametrize(
    "value", ["=1+1", "+SUM(1,1)", "-2+3", "@SUM(A1)", "  =1", "\t=1", "\n=1"]
)
def test_csv_formula_cells_are_literal(value):
    assert export._csv_cell(value).startswith("'")


def test_process_pdf_and_csv_are_readable(workspace, tmp_path, preview_fonts):
    store, _, card = workspace
    content = {
        **card["content"],
        "claim": '=HYPERLINK("https://example.org")',
        "limitations": "<b>不是HTML</b> & 原文边界",
    }
    evidence.edit_card(store, card["id"], content)
    output = tmp_path / "process"
    result = export.export_process(store, output)
    assert result["blocked"] == []
    rows = list(csv.DictReader((output / "evidence.csv").open(encoding="utf-8-sig")))
    assert rows[0]["claim"].startswith("'=")
    reader = PdfReader(output / "process.pdf")
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert "研究过程记录" in text
    assert "Simple baselines" in text
    assert "<b>不是HTML</b>" in text
    assert "FULL_PAGE_ONLY_NOT_EXPORTED" not in text
    assert reader.metadata.get("/Author") in (None, "")


def test_process_pdf_uses_configured_fonts_without_macos_fonts(
    workspace, tmp_path, monkeypatch
):
    import reportlab

    font_dir = Path(reportlab.__file__).parent / "fonts"
    monkeypatch.setattr(export, "PREVIEW_SONG", (str(tmp_path / "missing-body.ttc"), 6))
    monkeypatch.setattr(
        export, "PREVIEW_HEI", (str(tmp_path / "missing-heading.ttc"), 1)
    )
    monkeypatch.setenv("RESEARCH_PREVIEW_BODY_FONT", str(font_dir / "Vera.ttf"))
    monkeypatch.setenv("RESEARCH_PREVIEW_HEADING_FONT", str(font_dir / "VeraBd.ttf"))
    output = tmp_path / "portable-process"
    result = export.export_process(workspace[0], output)
    assert result["blocked"] == []
    reader = PdfReader(output / "process.pdf")
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert "Simple baselines require evaluation on the target dataset." in " ".join(
        text.split()
    )
    assert "FULL_PAGE_ONLY_NOT_EXPORTED" not in text
    assert reader.metadata.get("/Author") in (None, "")

    monkeypatch.setenv("RESEARCH_PREVIEW_BODY_FONT", str(tmp_path / "missing.ttf"))
    failed_output = tmp_path / "missing-font-process"
    failed = export.export_process(workspace[0], failed_output)
    assert failed["blocked"]
    assert not (failed_output / "process.pdf").exists()


def test_without_fonts_only_docx_and_marked_preview(workspace, tmp_path, preview_fonts):
    store, _, _ = workspace
    root = tmp_path / "artifacts"
    result = export.export_case(store, root)
    assert result["formal_ready"] is False
    assert not (root / "submission" / "case.pdf").exists()
    assert any("SimSun" in reason for reason in result["blocked_reasons"])
    assert any("合法使用权" in reason for reason in result["blocked_reasons"])
    assert result["body_characters"] <= 900 and result["total_characters"] <= 1000
    text = "".join(
        page.extract_text() or "" for page in PdfReader(root / "case-preview.pdf").pages
    )
    assert export.PREVIEW in text
    assert export.character_count(text) <= 1000
    assert "不声称效率提升" in result["text"]
    with ZipFile(root / "case.docx") as archive:
        core = ET.fromstring(archive.read("docProps/core.xml"))
        assert len(core) == 0
        body = archive.read("word/document.xml").decode()
        assert 'w:eastAsia="宋体"' in body and 'w:eastAsia="黑体"' in body
        assert 'w:sz w:val="28"' in body and 'w:sz w:val="32"' in body
        assert 'w:line="420"' in body and 'w:firstLine="560"' in body


def test_preview_font_never_passes_as_simsun(preview_fonts):
    result = export.font_preflight(
        export.PREVIEW_SONG[0], export.PREVIEW_SONG[1], expected="SimSun"
    )
    assert result["embeddable"] is True
    assert result["matches_expected"] is False
    assert result["ok"] is False
    assert "STSongti-SC-Regular" in result["names"]["6"]


@pytest.mark.parametrize("bits", [2, 0x100, 0x200, 0x104])
def test_restricted_no_subsetting_bitmap_fonts_fail(tmp_path, monkeypatch, bits):
    import fontTools.ttLib

    class TestFont(dict):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Name:
        nameID = 6

        def toUnicode(self):
            return "SimSun"

    monkeypatch.setattr(
        fontTools.ttLib,
        "TTFont",
        lambda *args, **kwargs: TestFont(
            {
                "name": SimpleNamespace(names=[Name()]),
                "OS/2": SimpleNamespace(fsType=bits),
                "glyf": {},
            }
        ),
    )
    path = tmp_path / "controlled.ttf"
    path.write_bytes(b"controlled-font-reader-fixture")
    result = export.font_preflight(path, expected="SimSun")
    assert result["ok"] is False and result["embeddable"] is False
    assert path.read_bytes() == b"controlled-font-reader-fixture"


@pytest.mark.parametrize("index", [-1, True, "0"])
def test_font_index_requires_nonnegative_integer(tmp_path, index):
    with pytest.raises(export.ExportError):
        export.font_preflight(tmp_path / "font.ttc", index)


def test_incomplete_case_cannot_receive_human_release(workspace, tmp_path):
    store, _, _ = workspace
    with pytest.raises(export.ExportError, match="科研门禁"):
        export.record_case_review(
            store,
            tmp_path / "artifacts",
            content_reviewed=True,
            anonymity_confirmed=True,
        )
    assert not (tmp_path / "artifacts" / "case-review.json").exists()


def test_drafts_cannot_write_submission(workspace, tmp_path):
    store, _, _ = workspace
    with pytest.raises(export.ExportError, match="submission"):
        export.export_process(store, tmp_path / "submission")
    with pytest.raises(export.ExportError, match="submission"):
        export.export_case(store, tmp_path / "submission")


def test_symlink_output_and_parent_are_rejected(workspace, tmp_path):
    store, _, _ = workspace
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(export.ExportError, match="符号链接"):
        export.export_process(store, alias / "nested")
    root = tmp_path / "process"
    root.mkdir()
    (root / "process.json").symlink_to(target / "outside.json")
    with pytest.raises(export.ExportError, match="符号链接"):
        export.export_process(store, root)
    assert not (target / "outside.json").exists()


def test_preview_is_rejected_by_submission_pdf_check(tmp_path, preview_fonts):
    snapshot = export.process_snapshot(Store(tmp_path / "data"))
    paragraphs, notes, _ = export._case_content(snapshot)
    result = export._check_pdf_bytes(
        export._pdf(export.TITLE, [("", p) for p in paragraphs + notes])
    )
    assert result["ok"] is False
    assert any("预览" in reason for reason in result["reasons"])
    assert any("字体" in reason for reason in result["reasons"])


def technical_submission(tmp_path, monkeypatch):
    from research_app import video

    root = tmp_path / "submission"
    root.mkdir()
    # 受控字节只用于包装边界单元测试，技术检测器被隔离；不代表有效PDF/视频。
    (root / "case.pdf").write_bytes(b"unit-pdf")
    (root / "case.mp4").write_bytes(b"unit-video")
    monkeypatch.setattr(
        export, "_check_pdf_bytes", lambda data: {"ok": True, "reasons": []}
    )
    monkeypatch.setattr(video, "check_video", lambda path: {"ok": True, "reasons": []})
    return root


def test_technical_pass_never_substitutes_human_or_current_research(
    tmp_path, monkeypatch
):
    root = technical_submission(tmp_path, monkeypatch)
    result = export.check_submission(root)
    assert result["technical_ok"] is True
    assert result["research_ok"] is False
    assert result["human_review_confirmed"] is False
    assert result["ok"] is False
    with pytest.raises(export.ExportError):
        export.record_submission_review(
            root,
            pdf_reviewed=True,
            video_reviewed=True,
            anonymity_confirmed=True,
            claims_confirmed=True,
        )


def test_human_signature_is_bound_to_both_files(tmp_path, monkeypatch):
    root = technical_submission(tmp_path, monkeypatch)
    monkeypatch.setattr(export, "_research_gate", lambda *args: [])
    export.record_submission_review(
        root,
        pdf_reviewed=True,
        video_reviewed=True,
        anonymity_confirmed=True,
        claims_confirmed=True,
    )
    assert export.check_submission(root)["ok"] is True
    (root / "case.mp4").write_bytes(b"changed-video")
    result = export.check_submission(root)
    assert result["human_review_confirmed"] is False
    assert result["ok"] is False


def test_research_gate_binds_current_snapshot_and_provenance(tmp_path, monkeypatch):
    root = tmp_path / "submission"
    root.mkdir()
    payload = {"process_sha256": "current", "blocked": [], "title": export.TITLE}
    monkeypatch.setattr(export, "_case_payload", lambda store: (payload, {}))
    content_hash = export._digest(export._bytes(payload))
    (root / "case-provenance.json").write_bytes(
        export._bytes(
            {
                "pdf_sha256": "pdf",
                "process_sha256": "current",
                "content_sha256": content_hash,
                "fonts_licensed": True,
            }
        )
    )
    (tmp_path / "case-review.json").write_bytes(
        export._bytes(
            {
                "content_sha256": content_hash,
                "content_reviewed": True,
                "anonymity_confirmed": True,
                "confirmed_at": now(),
            }
        )
    )
    assert export._research_gate(object(), root, {"case.pdf": "pdf"}) == []
    payload["process_sha256"] = "changed-source"
    assert export._research_gate(object(), root, {"case.pdf": "pdf"})


@pytest.mark.parametrize(
    "school",
    [
        "",
        "../school",
        "school/name",
        "school\\name",
        "school\nname",
        "school\x00name",
        "x..y",
        "C:school",
        "校" * 100,
    ],
)
def test_invalid_school_cannot_create_package(tmp_path, school):
    with pytest.raises(export.ExportError):
        export.package_submission(tmp_path, school)


def test_package_is_anonymous_inside_and_never_overwrites(tmp_path, monkeypatch):
    root = technical_submission(tmp_path, monkeypatch)
    monkeypatch.setattr(export, "_research_gate", lambda *args: [])
    export.record_submission_review(
        root,
        pdf_reviewed=True,
        video_reviewed=True,
        anonymity_confirmed=True,
        claims_confirmed=True,
    )
    result = export.package_submission(root, "测试学校")
    assert result["submitted"] is False
    for filename in result["files"]:
        file = root / filename
        assert file.parent == root and filename.startswith("测试学校")
        assert "测试学校".encode() not in file.read_bytes()
    assert export.package_submission(root, "测试学校")["ok"] is True
    (root / result["files"][0]).write_bytes(b"existing-user-content")
    with pytest.raises(export.ExportError, match="拒绝覆盖"):
        export.package_submission(root, "测试学校")
    assert (root / result["files"][0]).read_bytes() == b"existing-user-content"


def test_case_experiment_numbers_only_come_from_verified_summary(
    workspace, monkeypatch
):
    store, _, _ = workspace
    run_id = "7e96346b-a7cf-4d54-b7af-0679105ed534"
    root = store.data_dir / "experiments" / run_id
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        encoded(
            {
                "actual_parsed_count": 123,
                "label_counts": {"ham": 100, "spam": 23},
                "sms_text": "NEVER_COPY_MANIFEST_EXTRAS",
            }
        )
    )
    summary = {
        "run_id": run_id,
        "status": "confirmed",
        "created_at": now(),
        "recommended_candidate": "word_lr",
        "snapshot_sha256": "hash",
        "frozen_selection": {
            "study_revision": 1,
            "chosen_candidate": "word_lr",
            "reason": "人工冻结",
        },
        "test_aggregates": [
            {
                "candidate": "word_lr",
                "budget": 1000,
                "seeds": [0, 1, 2, 3, 4],
                "metrics": {
                    "spam_f1": {"mean": 0.2345, "sample_std": 0.0123, "n": 5},
                    "spam_recall": {"mean": 0.1234, "sample_std": 0, "n": 5},
                    "ham_false_positive_rate": {
                        "mean": 0.0001,
                        "sample_std": 0,
                        "n": 5,
                    },
                },
            }
        ],
        "raw_text": "NEVER_COPY_SUMMARY_EXTRAS",
    }
    monkeypatch.setattr(
        export.experiment,
        "list_runs",
        lambda data_dir: [{"run_id": run_id, "verified": True}],
    )
    calls = []

    def verified(rid, data_dir):
        calls.append(rid)
        return summary

    monkeypatch.setattr(export.experiment, "verified_summary", verified)
    snapshot = export.process_snapshot(store)
    paragraphs, _, blocked = export._case_content(snapshot)
    assert calls == [run_id]
    assert "0.2345±0.0123" in "".join(paragraphs)
    assert "0.1234" in "".join(paragraphs)
    assert blocked  # 真实实验数值不代替证据人审及正式科研决定。
    assert "NEVER_COPY" not in json.dumps(snapshot)


def test_unverified_experiment_does_not_export_numbers(workspace, monkeypatch):
    store, _, _ = workspace
    monkeypatch.setattr(
        export.experiment,
        "list_runs",
        lambda data_dir: [{"run_id": "invalid-run", "verified": False}],
    )
    result = export.process_snapshot(store)
    assert result["experiments"] == [
        {
            "run_id": "invalid-run",
            "status": "unverified",
            "note": "完整性校验未通过，不导出指标",
        }
    ]


def test_imported_word_pdf_requires_rights_and_research(workspace, tmp_path):
    store, _, _ = workspace
    with pytest.raises(export.ExportError, match="合法使用权"):
        export.import_case_pdf(store, tmp_path / "not-yet.pdf")
    with pytest.raises(export.ExportError, match="科研门禁"):
        export.import_case_pdf(
            store, tmp_path / "not-yet.pdf", tmp_path / "artifacts", fonts_licensed=True
        )


def test_trailing_control_character_in_school_name_is_rejected(tmp_path):
    with pytest.raises(export.ExportError, match="控制字符"):
        export.package_submission(tmp_path, "测试学校\n")


@pytest.mark.parametrize("changed_body", [False, True])
def test_imported_word_pdf_accepts_layout_whitespace_not_changed_content(
    workspace, tmp_path, monkeypatch, preview_fonts, changed_body
):
    store, _, _ = workspace
    payload = {
        "title": export.TITLE,
        "paragraphs": ["正文说明：原文支持与适用边界需要人工核验。" * 8],
        "notes": ["AI使用声明：模型辅助，人负责核验。"],
        "process_sha256": "unit-current",
        "blocked": [],
    }
    monkeypatch.setattr(export, "_case_payload", lambda store: (payload, {}))
    # 字体及科学资格分别有独立门禁测试；这里只隔离Word排版换行对原文一致性的影响。
    monkeypatch.setattr(
        export, "_check_pdf_bytes", lambda data: {"ok": True, "reasons": []}
    )
    root = tmp_path / "artifacts"
    export.record_case_review(
        store, root, content_reviewed=True, anonymity_confirmed=True
    )
    body = payload["paragraphs"][0] + ("本句不在审核正文中。" if changed_body else "")
    data = export._pdf(
        export.TITLE, [("", body), ("", payload["notes"][0])], preview=False
    )
    source = tmp_path / "word-output.pdf"
    source.write_bytes(data)
    if changed_body:
        with pytest.raises(export.ExportError, match="全文"):
            export.import_case_pdf(store, source, root, fonts_licensed=True)
        assert not (root / "submission" / "case.pdf").exists()
    else:
        result = export.import_case_pdf(store, source, root, fonts_licensed=True)
        assert result["formal_ready"] is True
        assert (root / "submission" / "case.pdf").read_bytes() == data
