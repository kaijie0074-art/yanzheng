"""共享合成实验产物：真实执行固定管道与哈希核验，不代表公开SMS案例结果。"""

import shutil

import pytest

from research_app import experiment


@pytest.fixture(scope="session")
def synthetic_confirmed_sms(tmp_path_factory):
    root = tmp_path_factory.mktemp("confirmed-synthetic-sms")
    case = root / "case"
    case.mkdir()
    rows = []
    for index in range(1800):
        label, words = (
            ("spam", "prize reward offer")
            if index % 5 == 0
            else ("ham", "ordinary project meeting")
        )
        rows.append(f"{label}\t{words} item{index}\n")
    (case / "SMSSpamCollection").write_text("".join(rows), encoding="utf-8")
    (case / "readme").write_text(
        "Synthetic regression corpus, not the official collection of 5574 messages.",
        encoding="utf-8",
    )
    summary = experiment.compare(root)
    experiment.freeze_selection(
        summary["run_id"],
        reason="合成夹具显式冻结验证推荐",
        study_revision=1,
        data_dir=root,
    )
    experiment.confirm(data_dir=root, run_id=summary["run_id"])
    return root, experiment.verified_summary(summary["run_id"], data_dir=root)


@pytest.fixture
def attach_confirmed_sms(synthetic_confirmed_sms):
    root, summary = synthetic_confirmed_sms

    def attach(store):
        for name in ("case", "experiments"):
            shutil.copytree(root / name, store.data_dir / name, dirs_exist_ok=True)
        return experiment.verified_summary(summary["run_id"], data_dir=store.data_dir)

    return attach
