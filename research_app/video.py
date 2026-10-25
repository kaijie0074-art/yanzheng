"""真实窗口录屏与本人旁白的受控合成；技术检查不替代人工匿名审查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from .storage import Store

DEFAULT_FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
DEFAULT_FFPROBE = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
TARGET_SECONDS = 270
MAX_BYTES = 200_000_000
SEGMENTS = ((0, 30), (30, 65), (65, 145), (145, 200), (200, 245), (245, 270))


class VideoError(ValueError):
    """可公开显示的错误，不包含素材路径、字幕正文或底层命令日志。"""


def _safe_path(value: str | Path, *, existing: bool = False) -> Path:
    path = Path(value).expanduser()
    if ".." in path.parts or any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
        raise VideoError("文件路径不允许上级跳转或控制字符")
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise VideoError("素材和输出路径不允许符号链接")
    if existing:
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise VideoError("前置条件缺失：需要真实窗口录屏、本人旁白及SRT文件")
    return path


def _number(value, *, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise VideoError("媒体或研究数值无效")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise VideoError("媒体或研究数值无效") from None
    if (
        not math.isfinite(result)
        or result < 0
        or (maximum is not None and result > maximum)
    ):
        raise VideoError("媒体或研究数值超出允许范围")
    return result


def _run(
    argv: list[str], *, cwd: Path | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise VideoError(
            "媒体工具不可用或处理超时，请检查ffmpeg-full及真实素材"
        ) from None
    if result.returncode:
        raise VideoError(
            "媒体处理失败，请检查素材格式及ffmpeg-full字幕滤镜；未发布产物"
        )
    if len(result.stdout) > 2 * 1024 * 1024:
        raise VideoError("媒体探测输出超限")
    return result


def _probe(path: Path, ffprobe_path: str) -> dict:
    result = _run(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        data = json.loads(result.stdout)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("format"), dict)
            or not isinstance(data.get("streams"), list)
        ):
            raise ValueError
        if any(not isinstance(stream, dict) for stream in data["streams"]):
            raise ValueError
        return data
    except (ValueError, UnicodeError):
        raise VideoError("媒体探测未返回有效结构") from None


def _duration(data: dict, stream: dict | None = None) -> float:
    value = (stream or {}).get("duration")
    if value in (None, "N/A"):
        value = data["format"].get("duration")
    return _number(value)


def _metadata_safe(data: dict) -> bool:
    # 仅允许编码器产生的技术标签；未知标签不猜测为匿名。
    allowed = {
        "major_brand": re.compile(r"[A-Za-z0-9 ]{1,16}"),
        "minor_version": re.compile(r"\d{1,12}"),
        "compatible_brands": re.compile(r"[A-Za-z0-9 ]{1,100}"),
        "encoder": re.compile(r"(?:Lavf[\d.]+|Lavc[\d.]+(?: (?:libx264|aac))?)"),
        "language": re.compile(r"und"),
        "handler_name": re.compile(r"(?:VideoHandler|SoundHandler)"),
        "vendor_id": re.compile(r"(?:\[0\]\[0\]\[0\]\[0\]|0000)"),
    }
    for node in [data["format"], *data["streams"]]:
        tags = node.get("tags", {})
        if not isinstance(tags, dict):
            return False
        for key, value in tags.items():
            if value in (None, ""):
                continue
            if (
                key.lower() not in allowed
                or not isinstance(value, str)
                or not allowed[key.lower()].fullmatch(value)
            ):
                return False
    return True


def check_video(path: str | Path, ffprobe_path: str = DEFAULT_FFPROBE) -> dict:
    """实测格式。ok只代表技术合规，不证明录屏真实、字幕同步或内容匿名。"""
    source = _safe_path(path, existing=True)
    data = _probe(source, ffprobe_path)
    video = [s for s in data["streams"] if s.get("codec_type") == "video"]
    audio = [s for s in data["streams"] if s.get("codec_type") == "audio"]
    reasons = []
    v = video[0] if video else {}
    a = audio[0] if audio else {}
    if len(video) != 1 or len(audio) != 1 or len(data["streams"]) != 2:
        reasons.append("必须仅有一条视频流及一条音频流")
    if (
        v.get("codec_name") != "h264"
        or v.get("width") != 1920
        or v.get("height") != 1080
    ):
        reasons.append("视频必须为1920×1080的H.264")
    if v.get("disposition", {}).get("attached_pic", 0):
        reasons.append("封面图片不是实际视频流")
    if a.get("codec_name") != "aac":
        reasons.append("音频必须为AAC")
    duration = _duration(data)
    if not 255 <= duration <= 285:
        reasons.append("成片时长必须为255至285秒")
    for stream in (v, a):
        if stream and not 255 <= _duration(data, stream) <= 285:
            reasons.append("音视频流时长必须均为255至285秒")
    size = source.stat().st_size
    if size > MAX_BYTES or size == 0:
        reasons.append("成片大小必须大于零且不超过200000000字节")
    if "mp4" not in data["format"].get("format_name", "").split(","):
        reasons.append("成片必须使用MP4容器")
    if not _metadata_safe(data):
        reasons.append("媒体含未知或潜在身份元数据，需清理后重新检查")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "duration_seconds": duration,
        "size_bytes": size,
        "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name"),
        "width": v.get("width"),
        "height": v.get("height"),
        "human_review_required": True,
        "human_review_note": "必须完整观看，人工确认真实操作、本人旁白、硬字幕、音画同步及匿名；技术检查不能替代。",
    }


def _stamp(milliseconds: int) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def _srt(segments: list[dict]) -> str:
    rows = []
    index = 1
    for segment in segments:
        chunks = []
        for sentence in re.findall(r"[^。！？；]+[。！？；]?", segment["narration"]):
            chunks.extend(
                sentence[pos : pos + 28] for pos in range(0, len(sentence), 28)
            )
        total = sum(map(len, chunks))
        used = 0
        start = segment["start_seconds"] * 1000
        span = (segment["end_seconds"] - segment["start_seconds"]) * 1000
        for chunk in chunks:
            begin = start + span * used // total
            used += len(chunk)
            end = start + span * used // total
            rows.append(f"{index}\n{_stamp(begin)} --> {_stamp(end)}\n{chunk}\n")
            index += 1
    return "\n".join(rows)


def _validate_srt(path: Path) -> bytes:
    if path.stat().st_size > 256 * 1024:
        raise VideoError("字幕文件超过256KiB")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").strip()
    except (OSError, UnicodeError):
        raise VideoError("字幕必须为UTF-8 SRT") from None
    end = 0
    blocks = re.split(r"\n\s*\n", text)
    pattern = (
        r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})"
    )
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0] != str(index):
            raise VideoError("字幕须按连续编号使用标准SRT格式")
        match = re.fullmatch(pattern, lines[1])
        if not match:
            raise VideoError("字幕时间格式无效")
        n = list(map(int, match.groups()))
        start = ((n[0] * 60 + n[1]) * 60 + n[2]) * 1000 + n[3]
        stop = ((n[4] * 60 + n[5]) * 60 + n[6]) * 1000 + n[7]
        if start != end or stop <= start or stop > TARGET_SECONDS * 1000:
            raise VideoError("字幕时间轴必须无重叠无缺口覆盖0至270秒")
        body = "\n".join(lines[2:])
        if (
            not body.strip()
            or any(c in body for c in "{}<>\\")
            or any(ord(c) < 32 and c != "\n" for c in body)
        ):
            raise VideoError("字幕仅接受普通文本，不接受样式或控制指令")
        end = stop
    if end != TARGET_SECONDS * 1000:
        raise VideoError("字幕时间轴必须覆盖270秒")
    return raw


def _publish(path: Path, content: bytes) -> None:
    target = _safe_path(path)
    if target.exists():
        if target.is_file() and target.read_bytes() == content:
            return
        raise VideoError("输出已存在且内容不同，请选择新目录；不会覆盖")
    try:
        with target.open("xb") as handle:
            handle.write(content)
    except OSError:
        raise VideoError("无法安全创建输出文件") from None


def _facts(store: Store) -> dict:
    from .evaluation import list_evaluations
    from .evidence import eligible_card, list_cards
    from .experiment import list_runs, verified_summary

    brief = store.latest_brief()
    revision = brief["revision"] if brief else None
    cards = list_cards(store, revision=revision) if brief else []
    source_checks = {}
    facts = {
        "study_revision": revision,
        "evidence_count": len(cards),
        "unreviewed_count": sum(c["review_status"] == "unreviewed" for c in cards),
        "eligible_count": sum(
            eligible_card(
                store, c["id"], revision=revision, source_checks=source_checks
            )
            for c in cards
        ),
        "experiment": None,
        "evaluation": None,
    }
    for run in list_runs(store.data_dir):
        if not run["verified"] or run["status"] != "confirmed":
            continue
        summary = verified_summary(run["run_id"], data_dir=store.data_dir)
        if summary.get("frozen_selection", {}).get("study_revision") != revision:
            continue
        chosen = summary["frozen_selection"]["chosen_candidate"]
        if chosen not in {"word_nb", "word_lr"}:
            continue
        for aggregate in summary["test_aggregates"]:
            if aggregate["candidate"] != chosen or aggregate["budget"] != 1000:
                continue
            metrics = aggregate["metrics"]
            facts["experiment"] = {
                "candidate": chosen,
                "budget": 1000,
                "seed_count": len(aggregate["seeds"]),
                "spam_f1": _number(metrics["spam_f1"]["mean"], maximum=1),
                "spam_f1_sample_std": _number(
                    metrics["spam_f1"]["sample_std"], maximum=1
                ),
                "spam_recall": _number(metrics["spam_recall"]["mean"], maximum=1),
                "ham_false_positive_rate": _number(
                    metrics["ham_false_positive_rate"]["mean"], maximum=1
                ),
            }
            break
        if facts["experiment"]:
            break
    for record in list_evaluations(store):
        if not record["current_revision"]:
            continue
        direct = record["methods"]["direct"]
        workbench = record["methods"]["workbench"]
        facts["evaluation"] = {
            "status": "complete" if record["status"] == "complete" else "pending",
            "direct_success": direct["status"] == "success",
            "workbench_success": workbench["status"] == "success",
            "direct_questions": len(direct.get("answers", [])),
            "direct_claims": sum(len(a["claims"]) for a in direct.get("answers", [])),
            "direct_model_seconds": _number(direct["model_seconds"])
            if direct.get("model_seconds") is not None
            else None,
            "pending_claim_count": int(record["pending_claim_count"]),
        }
        break
    return facts


def prepare_video_assets(
    store: Store, output_dir: str | Path = "artifacts/video"
) -> dict:
    """只生成匿名提词稿、分镜JSON及初始SRT；不生成任何视频或音频。"""
    root = _safe_path(output_dir)
    if "submission" in root.parts:
        raise VideoError("分镜和字幕草稿不得放进正式submission目录")
    facts = _facts(store)
    experiment = facts["experiment"]
    if experiment:
        name = "逻辑回归" if experiment["candidate"] == "word_lr" else "朴素贝叶斯"
        result = (
            f"当前已经确认的历史运行选择{name}，训练预算为一千条，使用{experiment['seed_count']}个固定种子。"
            f"保留集垃圾短信F1均值为{experiment['spam_f1']:.4f}，样本标准差为{experiment['spam_f1_sample_std']:.4f}；"
            f"召回率为{experiment['spam_recall']:.2%}，正常短信误报率为{experiment['ham_false_positive_rate']:.2%}。"
            "这些数字来自通过完整性核对的实验文件，不是模型生成。低召回不能被零误报掩盖，样本标准差也不是独立测试集置信区间。"
        )
    else:
        result = "当前修订尚无可核验的已确认保留集结果，因此这里不展示预填分数，也不宣称已有最佳方案。待真实实验及人工冻结完成后，应重新生成脚本。"
    evaluation = facts["evaluation"]
    if evaluation is None:
        evaluation_text = "当前尚无本修订的成对评价记录，六题比较和独立人工审核仍待完成。不能把技术可运行解释为研究价值已验证。"
    else:
        timing = (
            f"模型调用实耗时{evaluation['direct_model_seconds']:.2f}秒，"
            if evaluation["direct_model_seconds"] is not None
            else ""
        )
        evaluation_text = (
            f"当前直接问答保存了{evaluation['direct_questions']}题、{evaluation['direct_claims']}条重要主张，{timing}这不是完整流程总耗时。"
            + (
                "工作台侧尚未成功完成，"
                if not evaluation["workbench_success"]
                else "工作台侧已生成记录，"
            )
            + (
                f"仍有{evaluation['pending_claim_count']}条已生成主张待人工审核，整体评价未完成。"
                if evaluation["status"] == "pending"
                else "成对评价和逐条复核已有记录，但本视频不从状态标签推断提升幅度。"
            )
        )
    narration = [
        "如果人工智能给出了一段听起来很专业的研究建议，我们怎样知道它有依据？这个工作台叫研证，定位是科研选题论证与文献证据核验，而不是代写论文。公开案例关注有限训练标注条件下，词频逆文档频率特征搭配朴素贝叶斯还是逻辑回归，更适合作为英文垃圾短信识别基线。我们展示的是可追溯过程，不是发明新算法。",
        "先明确研究对象、资源限制与验证目标，再形成检索式。检索返回的书目元数据不等于读过全文，更不等于质量认证。演示材料使用公开数据页和两篇公开方法论文，并保留正式来源入口与访问层级。它们只是一个案例材料包，不是穷尽检索或系统综述。只有合法取得并导入的全文才能建立物理页码；通用文本分类论文的结果不能直接当作短信分类成绩。",
        (
            "现在沿着来源、页码、原句和适用条件查看一张证据卡。模型只能基于选定页面提取主张，程序核对引用是否存在，但字符串命中并不说明作者支持当前推论。复核人需要打开原PDF，查看上下文、实验对象和限制，再独立写下判断。"
            f"当前修订共有{facts['evidence_count']}张证据卡，其中{facts['unreviewed_count']}张尚未人工复核，动态复核后可作为正式论据的有{facts['eligible_count']}张。"
            "如果没有合格论据，工作台只能留下未完成核验的过程记录，不能把模型建议变成正式科研决定。即使原句完全一致，也可能被判为不支持、部分支持或证据不足。修改主张、引文或来源会使旧复核失效；研究问题改变后也不能沿用旧决定。这里应展示真实状态，而不是为了视频好看替复核人勾选通过。"
        ),
        "方案比较只讨论两个预设基线，不执行模型生成代码。两条管道使用相同划分和样本，词表及逆文档频率只在训练子集拟合。验证集先择优，人工冻结后才评估保留集，不能看完测试结果再换赢家。"
        + result,
        "应用价值需要另外检验。我们预先固定六个问题，用同一实际模型和相同材料比较直接问答与工作台流程，逐条记录来源定位、原文支持、适用边界和人工修正。"
        + evaluation_text
        + "生成者不能给自己打正确率分数。人工审核时间要纳入总用时；这只是单案例的小样本观察，不支持普遍效率提升或显著性结论。",
        "人工智能帮助整理问题、检索思路和候选解释，人负责核验依据与承担决定。应用在本机运行不等于模型离线运行，使用外部模型前需确认材料授权。短信正文不发送给模型，也不进入参赛导出。历史英文数据的结果不能外推成当代中文反诈能力。让尚未完成的核验保持未完成，也是可信科研的一部分。",
    ]
    visuals = [
        "【录屏-主人】仅展示本地工作台研究问题与约束，不显示桌面、地址用户路径或模型设置。",
        "【录屏-主人】展示真实检索历史、来源层级和正式入口，历史结果明确标为回放。",
        "【录屏-主人】打开真实证据卡及对应原PDF页，展示当前待复核状态；不得代替本人作支持判断。",
        "【录屏-主人】展示固定管道、冻结记录与实际实验指标；未运行则展示待运行。",
        "【录屏-主人】展示六题及当前成对评价状态；pending保持pending，不演造纠错或胜利剧情。",
        "【录屏-主人】回到工作台限制说明，保持窗口匿名，不展示账户、个人目录或短信正文。",
    ]
    segments = [
        {
            "start_seconds": start,
            "end_seconds": end,
            "visual": visual,
            "narration": words,
        }
        for (start, end), visual, words in zip(
            SEGMENTS, visuals, narration, strict=True
        )
    ]
    instructions = (
        "录制前置：由作品负责人在macOS按Shift-Command-5，选择仅覆盖应用窗口的区域录制；"
        "不要录整屏。录屏与麦克风权限由本人授予。隐藏桌面、账户、学校、姓名、文件路径与API设置。"
        "本人朗读旁白，录制至少270秒真实操作和至少270秒旁白音轨；不足时重新采集，不循环或填充静帧。"
        "分镜与SRT为节奏草稿，必须按实际朗读核对字幕时间；本程序不自动录屏、不合成人声。"
        "完整观看成片，核对匿名、事实、硬字幕和音画同步后才能进入正式提交。"
    )
    payload = {
        "duration_seconds": TARGET_SECONDS,
        "status": "recording_and_human_review_required",
        "instructions": instructions,
        "facts": facts,
        "segments": segments,
    }
    text = (
        instructions
        + "\n\n"
        + "\n\n".join(
            f"{s['start_seconds']}—{s['end_seconds']}秒\n{s['visual']}\n{s['narration']}"
            for s in segments
        )
        + "\n"
    )
    content = {
        "storyboard.json": (
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode(),
        "narration.txt": text.encode(),
        "captions.srt": _srt(segments).encode(),
    }
    root.mkdir(parents=True, exist_ok=True)
    # 全部预检后再落盘，已存在的不同脚本必须改用新目录。
    for name, data in content.items():
        target = _safe_path(root / name)
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise VideoError("脚本目录已有不同内容，请选择新目录")
    for name, data in content.items():
        _publish(root / name, data)
    return {**payload, "files": list(content), "video_created": False}


def assemble_video(
    screen_path: str | Path,
    voice_path: str | Path,
    subtitle_path: str | Path,
    output_path: str | Path = "artifacts/submission/case.mp4",
    *,
    ffmpeg_path: str = DEFAULT_FFMPEG,
    ffprobe_path: str = DEFAULT_FFPROBE,
) -> dict:
    """合成用户提供的真实素材，不生成/循环/拉长素材；先验证临时文件再独占发布。"""
    screen = _safe_path(screen_path, existing=True)
    voice = _safe_path(voice_path, existing=True)
    subtitles = _safe_path(subtitle_path, existing=True)
    output = _safe_path(output_path)
    inputs = (screen, voice, subtitles)
    if (
        len({(p.stat().st_dev, p.stat().st_ino) for p in inputs}) != 3
        or output in inputs
    ):
        raise VideoError("录屏、旁白、字幕及输出必须使用不同文件")
    if output.suffix.lower() != ".mp4":
        raise VideoError("输出文件扩展名必须为.mp4")
    if output.exists():
        raise VideoError("输出已存在，请使用新文件名；不会覆盖现成产物")
    captions = _validate_srt(subtitles)
    screen_info = _probe(screen, ffprobe_path)
    voice_info = _probe(voice, ffprobe_path)
    video_streams = [
        s
        for s in screen_info["streams"]
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic", 0)
    ]
    audio_streams = [s for s in voice_info["streams"] if s.get("codec_type") == "audio"]
    if not video_streams or not audio_streams:
        raise VideoError("前置条件缺失：录屏须含真实视频流，旁白须含音频流")
    if (
        _duration(screen_info, video_streams[0]) < TARGET_SECONDS
        or _duration(voice_info, audio_streams[0]) < TARGET_SECONDS
    ):
        raise VideoError("真实录屏和本人旁白都必须至少270秒；禁止补静帧、循环或拉伸")
    output.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(output.parent)
    attempts = 0
    bitrate = 3_500_000
    with tempfile.TemporaryDirectory(prefix=".video-", dir=output.parent) as folder:
        working = Path(folder)
        (working / "captions.srt").write_bytes(captions)
        temporary = working / "render.mp4"
        # 字幕路径只使用私有工作目录中的固定字面量；用户路径不会进入滤镜表达式。
        video_filter = (
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            "subtitles=filename=captions.srt:force_style='FontName=Heiti SC,FontSize=22,MarginV=30'"
        )
        for attempt in range(2):
            attempts += 1
            argv = [
                str(ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file",
                "-i",
                str(screen),
                "-protocol_whitelist",
                "file",
                "-i",
                str(voice),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-t",
                str(TARGET_SECONDS),
                "-vf",
                video_filter,
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                str(bitrate),
                "-maxrate",
                str(bitrate),
                "-bufsize",
                str(bitrate * 2),
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-map_metadata",
                "-1",
                "-map_metadata:s:v",
                "-1",
                "-map_metadata:s:a",
                "-1",
                "-map_chapters",
                "-1",
                "-metadata:s:v:0",
                "handler_name=VideoHandler",
                "-metadata:s:a:0",
                "handler_name=SoundHandler",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
            _run(argv, cwd=working, timeout=1800)
            result = check_video(temporary, ffprobe_path=ffprobe_path)
            if result["ok"]:
                break
            if attempt == 0 and result["size_bytes"] > MAX_BYTES:
                # 根据实际大小缩小一次并保留容器开销余量，不因结果内容反复美化。
                bitrate = max(
                    100_000,
                    min(
                        int(bitrate * MAX_BYTES / result["size_bytes"] * 0.90),
                        int((MAX_BYTES * 0.90 * 8 / TARGET_SECONDS) - 128_000),
                    ),
                )
                continue
            raise VideoError("成片技术检查未通过：" + "；".join(result["reasons"]))
        if not result["ok"]:
            raise VideoError("降低一次码率后仍不合规，未发布成片")
        with temporary.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        _safe_path(output)
        try:
            # 同文件系统原子、独占发布，竞态中的现成文件也不会被覆盖。
            os.link(temporary, output)
        except OSError:
            raise VideoError("无法独占发布成片；目标可能已经存在，未覆盖") from None
    return {
        **result,
        "sha256": digest,
        "encoding_attempts": attempts,
        "video_bitrate": bitrate,
        "audio_bitrate": 128_000,
        "status": "technical_pass_human_review_required",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只合成真实macOS窗口录屏与本人旁白；不自动录制。请用Shift-Command-5仅录应用区域，先隐藏身份信息。"
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="生成270秒分镜、本人朗读稿和待对齐SRT，不生成视频",
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--assets-dir", default="artifacts/video")
    parser.add_argument("--screen", help="至少270秒的真实窗口录屏")
    parser.add_argument("--voice", help="至少270秒的本人旁白录音")
    parser.add_argument("--subtitles", help="已按本人朗读核对的UTF-8 SRT")
    parser.add_argument("--output", default="artifacts/submission/case.mp4")
    parser.add_argument(
        "--check", metavar="MP4", help="仅进行媒体技术检查，不代替人工完整观看"
    )
    args = parser.parse_args(argv)
    try:
        if (
            sum(
                (
                    bool(args.prepare),
                    bool(args.check),
                    bool(args.screen or args.voice or args.subtitles),
                )
            )
            != 1
        ):
            raise VideoError(
                "请选择--prepare、--check或完整的--screen/--voice/--subtitles合成参数"
            )
        if args.prepare:
            result = prepare_video_assets(Store(args.data_dir), args.assets_dir)
        elif args.check:
            result = check_video(args.check)
        elif not all((args.screen, args.voice, args.subtitles)):
            raise VideoError("前置条件缺失：必须提供真实窗口录屏、本人旁白与字幕")
        else:
            result = assemble_video(
                args.screen, args.voice, args.subtitles, args.output
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result.get("ok", True) else 1
    except VideoError as exc:
        print(json.dumps({"ok": False, "reasons": [str(exc)]}, ensure_ascii=False))
        return 1
    except (ValueError, OSError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "reasons": [
                        "操作未完成：请检查真实素材、270秒时轴、路径安全、研究完整性和媒体工具；不输出底层敏感日志。"
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
