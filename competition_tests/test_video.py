"""只使用自造元数据和临时文件；不把测试素材当作比赛录屏。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from research_app import video
from research_app.storage import Store


def media_info(*, duration=270, video_codec="h264", audio_codec="aac"):
    return {
        "format": {
            "duration": str(duration),
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "tags": {
                "major_brand": "isom",
                "minor_version": "512",
                "compatible_brands": "isomiso2avc1mp41",
                "encoder": "Lavf62.3.100",
            },
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "width": 1920,
                "height": 1080,
                "duration": str(duration),
                "tags": {
                    "language": "und",
                    "handler_name": "VideoHandler",
                    "vendor_id": "[0][0][0][0]",
                    "encoder": "Lavc62.11.100 libx264",
                },
            },
            {
                "codec_type": "audio",
                "codec_name": audio_codec,
                "duration": str(duration),
                "tags": {
                    "language": "und",
                    "handler_name": "SoundHandler",
                    "vendor_id": "[0][0][0][0]",
                },
            },
        ],
    }


@pytest.fixture
def media(tmp_path):
    screen = tmp_path / "screen.mov"
    voice = tmp_path / "voice.wav"
    subtitles = tmp_path / "字幕',evil-filter.srt"
    screen.write_bytes(b"test-only-screen")
    voice.write_bytes(b"test-only-audio")
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:04:30,000\n这是测试字幕，不是比赛内容。\n",
        encoding="utf-8",
    )
    return screen, voice, subtitles, tmp_path / "result.mp4"


def test_check_reports_technical_only(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fake-media-for-mocked-probe")
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())
    result = video.check_video(path)
    assert result["ok"] is True
    assert result["duration_seconds"] == 270
    assert result["size_bytes"] == path.stat().st_size
    assert result["human_review_required"] is True
    assert "人工" in result["human_review_note"]


@pytest.mark.parametrize(
    "change,expected",
    [
        (lambda d: d["format"].update(duration="254.99"), "255至285"),
        (lambda d: d["format"].update(duration="285.01"), "255至285"),
        (lambda d: d["streams"][0].update(codec_name="hevc"), "H.264"),
        (lambda d: d["streams"][0].update(width=1280), "1920"),
        (lambda d: d["streams"][1].update(codec_name="mp3"), "AAC"),
        (lambda d: d["streams"][1].update(duration="30"), "音视频流"),
        (lambda d: d["streams"].pop(), "音频流"),
        (lambda d: d["streams"].append({"codec_type": "subtitle"}), "仅有"),
        (lambda d: d["streams"][0].update(disposition={"attached_pic": 1}), "封面图片"),
        (lambda d: d["format"].update(format_name="matroska,webm"), "MP4"),
        (lambda d: d["format"]["tags"].update(artist="private person"), "元数据"),
        (lambda d: d["streams"][0]["tags"].update(title="school"), "元数据"),
        (lambda d: d["streams"][0]["tags"].update(encoder="/Users/secret"), "元数据"),
    ],
)
def test_check_rejects_noncompliance(tmp_path, monkeypatch, change, expected):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"mocked")
    info = media_info()
    change(info)
    monkeypatch.setattr(video, "_probe", lambda *_: info)
    result = video.check_video(path)
    assert result["ok"] is False
    assert expected in " ".join(result["reasons"])
    assert "private person" not in json.dumps(result)


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", None, True])
def test_check_rejects_invalid_duration(tmp_path, monkeypatch, value):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"mocked")
    info = media_info()
    info["format"]["duration"] = value
    monkeypatch.setattr(video, "_probe", lambda *_: info)
    with pytest.raises(video.VideoError):
        video.check_video(path)


def test_actual_size_not_probe_claim(tmp_path, monkeypatch):
    path = tmp_path / "large.mp4"
    with path.open("wb") as handle:
        handle.truncate(video.MAX_BYTES + 1)
    info = media_info()
    info["format"]["size"] = "1"
    monkeypatch.setattr(video, "_probe", lambda *_: info)
    result = video.check_video(path)
    assert not result["ok"]
    assert result["size_bytes"] == video.MAX_BYTES + 1
    assert "200000000" in " ".join(result["reasons"])


@pytest.mark.parametrize("kind", ["file", "parent", "traversal", "control"])
def test_unsafe_paths_rejected(tmp_path, kind):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"source")
    if kind == "file":
        path = tmp_path / "link.mp4"
        path.symlink_to(original)
    elif kind == "parent":
        directory = tmp_path / "linkdir"
        directory.symlink_to(tmp_path, target_is_directory=True)
        path = directory / "original.mp4"
    elif kind == "traversal":
        path = tmp_path / "sub" / ".." / "original.mp4"
    else:
        path = tmp_path / "bad\n.mp4"
    with pytest.raises(video.VideoError):
        video.check_video(path)


def test_missing_real_sources_do_not_create_video(tmp_path):
    target = tmp_path / "submission" / "case.mp4"
    with pytest.raises(video.VideoError, match="前置条件缺失"):
        video.assemble_video(
            tmp_path / "missing.mov",
            tmp_path / "missing.wav",
            tmp_path / "missing.srt",
            target,
        )
    assert not target.exists()
    assert not target.parent.exists()


@pytest.mark.parametrize("short_source", ["screen", "voice"])
def test_short_source_never_padded(media, monkeypatch, short_source):
    screen, voice, subtitles, output = media

    def probe(path, _):
        return media_info(
            duration=269.9
            if path == (screen if short_source == "screen" else voice)
            else 270
        )

    monkeypatch.setattr(video, "_probe", probe)
    monkeypatch.setattr(
        video, "_run", lambda *a, **k: pytest.fail("不得调用编码器补帧")
    )
    with pytest.raises(video.VideoError, match="至少270秒"):
        video.assemble_video(screen, voice, subtitles, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "text",
    [
        "1\n00:00:00,000 --> 00:04:29,000\n短字幕\n",
        "1\n00:00:01,000 --> 00:04:30,000\n有缺口\n",
        "1\n00:00:00,000 --> 00:04:30,000\n{\\an8}样式覆盖\n",
        "1\n00:00:00,000 --> 00:04:30,000\n<b>标记</b>\n",
        "2\n00:00:00,000 --> 00:04:30,000\n编号不连续\n",
    ],
)
def test_bad_srt_is_rejected(media, text):
    screen, voice, subtitles, output = media
    subtitles.write_text(text, encoding="utf-8")
    with pytest.raises(video.VideoError):
        video.assemble_video(screen, voice, subtitles, output)
    assert not output.exists()


def test_existing_output_and_input_collision(media):
    screen, voice, subtitles, output = media
    output.write_bytes(b"user-existing-result")
    with pytest.raises(video.VideoError, match="输出已存在"):
        video.assemble_video(screen, voice, subtitles, output)
    assert output.read_bytes() == b"user-existing-result"
    with pytest.raises(video.VideoError, match="不同文件"):
        video.assemble_video(screen, voice, subtitles, screen)
    alias = screen.parent / "alias.wav"
    import os

    os.link(screen, alias)
    with pytest.raises(video.VideoError, match="不同文件"):
        video.assemble_video(screen, alias, subtitles, screen.parent / "new.mp4")


def test_assembly_fixed_filter_metadata_and_atomic_publication(media, monkeypatch):
    screen, voice, subtitles, output = media
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())
    calls = []

    def run(argv, *, cwd=None, timeout=120):
        calls.append(argv)
        assert not output.exists()
        assert cwd != screen.parent
        assert (cwd / "captions.srt").read_bytes() == subtitles.read_bytes()
        assert str(subtitles) not in argv[argv.index("-vf") + 1]
        assert "filename=captions.srt" in argv[argv.index("-vf") + 1]
        assert "-stream_loop" not in argv and "tpad" not in " ".join(argv)
        Path(argv[-1]).write_bytes(b"encoded mock video")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(video, "_run", run)
    originals = [p.read_bytes() for p in (screen, voice, subtitles)]
    result = video.assemble_video(screen, voice, subtitles, output)
    assert result["ok"]
    assert result["human_review_required"]
    assert result["encoding_attempts"] == 1
    assert result["video_bitrate"] == 3_500_000
    assert len(result["sha256"]) == 64
    assert calls[0][0] == video.DEFAULT_FFMPEG
    assert "-map_metadata:s:v" in calls[0] and "-map_chapters" in calls[0]
    assert output.read_bytes() == b"encoded mock video"
    assert [p.read_bytes() for p in (screen, voice, subtitles)] == originals
    assert not list(output.parent.glob(".video-*"))


def test_oversize_reencodes_once_using_measured_size(media, monkeypatch):
    screen, voice, subtitles, output = media
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())
    bitrates = []

    def run(argv, **kwargs):
        bitrates.append(int(argv[argv.index("-b:v") + 1]))
        with Path(argv[-1]).open("wb") as handle:
            handle.truncate(video.MAX_BYTES + 10 if len(bitrates) == 1 else 100)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(video, "_run", run)
    result = video.assemble_video(screen, voice, subtitles, output)
    assert result["encoding_attempts"] == 2
    assert bitrates[1] < bitrates[0]
    assert result["size_bytes"] == 100


def test_second_oversize_cleans_temp_and_does_not_publish(media, monkeypatch):
    screen, voice, subtitles, output = media
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        with Path(argv[-1]).open("wb") as handle:
            handle.truncate(video.MAX_BYTES + 10)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(video, "_run", run)
    with pytest.raises(video.VideoError):
        video.assemble_video(screen, voice, subtitles, output)
    assert len(calls) == 2
    assert not output.exists()
    assert not list(output.parent.glob(".video-*"))


def test_failed_encoder_cleans_temp(media, monkeypatch):
    screen, voice, subtitles, output = media
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())

    def fail(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"partial")
        raise video.VideoError("编码失败")

    monkeypatch.setattr(video, "_run", fail)
    with pytest.raises(video.VideoError):
        video.assemble_video(screen, voice, subtitles, output)
    assert not output.exists()
    assert not list(output.parent.glob(".video-*"))


def test_publish_race_does_not_overwrite(media, monkeypatch):
    screen, voice, subtitles, output = media
    monkeypatch.setattr(video, "_probe", lambda *_: media_info())

    def run(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"rendered")
        output.write_bytes(b"concurrent user file")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(video, "_run", run)
    with pytest.raises(video.VideoError, match="独占发布"):
        video.assemble_video(screen, voice, subtitles, output)
    assert output.read_bytes() == b"concurrent user file"


def test_process_no_shell_logs_not_disclosed(monkeypatch):
    def failed(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 1, b"", b"secret-user-path-and-key")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(video.VideoError) as error:
        video._run(["ffmpeg"])
    assert "secret" not in str(error.value)


def test_prepare_empty_store_is_pending_not_fabricated(tmp_path):
    store = Store(tmp_path / "data")
    root = tmp_path / "assets"
    result = video.prepare_video_assets(store, root)
    assert result["video_created"] is False
    assert result["facts"]["experiment"] is None
    assert result["facts"]["evaluation"] is None
    assert "尚无可核验" in (root / "narration.txt").read_text()
    assert "尚无本修订的成对评价" in (root / "narration.txt").read_text()
    assert [(s["start_seconds"], s["end_seconds"]) for s in result["segments"]] == list(
        video.SEGMENTS
    )
    video._validate_srt(root / "captions.srt")
    assert "00:00:00,000" in (root / "captions.srt").read_text()
    assert "00:04:30,000" in (root / "captions.srt").read_text()
    assert not list(root.rglob("*.mp4"))
    assert "本人" in result["instructions"] and "权限" in result["instructions"]


def test_prepare_uses_whitelisted_actual_numbers_pending_and_not_raw_text(
    tmp_path, monkeypatch
):
    from research_app import evaluation, evidence, experiment

    store = Store(tmp_path / "data")
    monkeypatch.setattr(
        store, "latest_brief", lambda: {"revision": 1, "content": "private identity"}
    )
    monkeypatch.setattr(
        evidence,
        "list_cards",
        lambda *a, **k: [
            {"id": str(i), "review_status": "unreviewed", "content": "private PDF"}
            for i in range(4)
        ],
    )
    monkeypatch.setattr(evidence, "eligible_card", lambda *a, **k: False)
    monkeypatch.setattr(
        experiment,
        "list_runs",
        lambda *_: [{"run_id": "id", "verified": True, "status": "confirmed"}],
    )
    summary = {
        "frozen_selection": {
            "study_revision": 1,
            "chosen_candidate": "word_lr",
            "reason": "private identity",
        },
        "test_aggregates": [
            {
                "candidate": "word_lr",
                "budget": 1000,
                "seeds": list(range(5)),
                "metrics": {
                    "spam_f1": {"mean": 0.181512662210657, "sample_std": 0.0262394613},
                    "spam_recall": {"mean": 0.10},
                    "ham_false_positive_rate": {"mean": 0},
                },
            }
        ],
        "output_text": "private model output",
        "sms": "private SMS",
    }
    monkeypatch.setattr(experiment, "verified_summary", lambda *a, **k: summary)
    record = {
        "current_revision": True,
        "status": "pending",
        "pending_claim_count": 18,
        "methods": {
            "direct": {
                "status": "success",
                "model_seconds": 49.328846,
                "answers": [
                    {"claims": ["private", "private", "private"]} for _ in range(6)
                ],
            },
            "workbench": {"status": "pending"},
        },
        "packet": "private full PDF",
    }
    monkeypatch.setattr(evaluation, "list_evaluations", lambda *_: [record])
    result = video.prepare_video_assets(store, tmp_path / "assets")
    words = (tmp_path / "assets" / "narration.txt").read_text()
    assert "0.1815" in words and "0.0262" in words
    assert "10.00%" in words and "0.00%" in words
    assert "49.33秒" in words and "6题、18条" in words
    assert "4张尚未人工复核" in words
    assert "整体评价未完成" in words
    assert result["facts"]["evaluation"]["status"] == "pending"
    for path in (tmp_path / "assets").iterdir():
        assert "private" not in path.read_text()
        assert str(tmp_path) not in path.read_text()
    video._validate_srt(tmp_path / "assets" / "captions.srt")
    summary["test_aggregates"][0]["metrics"]["spam_f1"]["mean"] = 0.42
    newer = video.prepare_video_assets(store, tmp_path / "new-assets")
    assert newer["facts"]["experiment"]["spam_f1"] == 0.42
    assert "0.4200" in (tmp_path / "new-assets" / "narration.txt").read_text()


def test_prepare_refuses_submission_and_conflicting_files(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(video.VideoError, match="submission"):
        video.prepare_video_assets(store, tmp_path / "submission")
    root = tmp_path / "assets"
    video.prepare_video_assets(store, root)
    video.prepare_video_assets(store, root)  # 相同内容可安全重用。
    (root / "narration.txt").write_text("本人已经修改的稿件")
    with pytest.raises(video.VideoError, match="不同内容"):
        video.prepare_video_assets(store, root)
    assert (root / "narration.txt").read_text() == "本人已经修改的稿件"


def test_cli_requires_real_sources(capsys):
    assert video.main(["--screen", "missing.mov"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
