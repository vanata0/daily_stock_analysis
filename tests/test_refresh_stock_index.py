# -*- coding: utf-8 -*-
"""Tests for scripts.refresh_stock_index default fetch behavior."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

refresh_stock_index = importlib.import_module("refresh_stock_index")


def test_main_fetches_tushare_with_a_rk_by_default():
    with (
        patch.object(refresh_stock_index, "_has_tushare_token", return_value=True),
        patch.object(refresh_stock_index, "_run") as run,
        patch.object(refresh_stock_index, "_sync_static_index"),
    ):
        exit_code = refresh_stock_index.main([])

    assert exit_code == 0
    assert run.call_args_list[0].args[0] == [
        sys.executable,
        "scripts/fetch_tushare_stock_list.py",
        "--a-rk",
    ]
    assert run.call_args_list[1].args[0] == [
        sys.executable,
        "scripts/generate_index_from_csv.py",
        "--source",
        "tushare",
    ]


def test_fetch_failure_reports_actionable_alternatives(capsys):
    """Tushare 接入下线后 token 仍在，抓取会失败。

    此时裸的 "command failed with exit code N" 无从下手，必须指出可用的
    替代路径，并说明运行时自动补全不受影响。
    """
    import subprocess

    def fake_run(cmd, *_a, **_kw):
        if any("fetch_tushare_stock_list.py" in part for part in cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return None

    with (
        patch.object(refresh_stock_index, "_has_tushare_token", return_value=True),
        patch.object(refresh_stock_index, "_run", side_effect=fake_run),
        patch.object(refresh_stock_index, "_sync_static_index"),
    ):
        exit_code = refresh_stock_index.main([])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--skip-fetch" in err
    assert "STOCK_INDEX_REMOTE_UPDATE_ENABLED" in err


def test_skip_fetch_bypasses_tushare_entirely(tmp_path):
    """Tushare 不可用时的替代路径：用现有 CSV 重新生成索引。"""
    csvs = []
    for name in ("stock_list_a.csv", "stock_list_hk.csv", "stock_list_us.csv"):
        f = tmp_path / name
        f.write_text("code,name\n000001,平安银行\n", encoding="utf-8")
        csvs.append(f)

    with (
        patch.object(refresh_stock_index, "_REQUIRED_CSV_PATHS", tuple(csvs)),
        patch.object(refresh_stock_index, "_run") as run,
        patch.object(refresh_stock_index, "_sync_static_index"),
    ):
        exit_code = refresh_stock_index.main(["--skip-fetch"])

    assert exit_code == 0
    called = [" ".join(c.args[0]) for c in run.call_args_list]
    assert not any("fetch_tushare_stock_list.py" in c for c in called)
    assert any("generate_index_from_csv.py" in c for c in called)


def test_skip_fetch_aborts_when_all_csv_missing(tmp_path, capsys):
    """CSV 全缺失时必须中止。

    generate_index_from_csv 对缺失 CSV 只 warn 后 continue，会只用 JP/KR 种子行
    生成残缺索引并正常退出——实测把 31678 条的线上索引覆盖成了 60 条，且退出码
    为 0，看起来像成功。这条路径必须在入口就拦住。
    """
    missing = tuple(tmp_path / n for n in
                    ("stock_list_a.csv", "stock_list_hk.csv", "stock_list_us.csv"))

    with (
        patch.object(refresh_stock_index, "_REQUIRED_CSV_PATHS", missing),
        patch.object(refresh_stock_index, "_run") as run,
        patch.object(refresh_stock_index, "_sync_static_index") as sync,
    ):
        exit_code = refresh_stock_index.main(["--skip-fetch"])

    assert exit_code == 2
    run.assert_not_called(), "中止后不应再执行任何生成命令"
    sync.assert_not_called()
    assert "已中止" in capsys.readouterr().err


def test_skip_fetch_warns_on_partially_missing_csv(tmp_path, capsys):
    """只缺部分 CSV 时继续执行，但要明确提示哪些市场会缺失。"""
    present = tmp_path / "stock_list_a.csv"
    present.write_text("code,name\n000001,平安银行\n", encoding="utf-8")
    paths = (present, tmp_path / "stock_list_hk.csv", tmp_path / "stock_list_us.csv")

    with (
        patch.object(refresh_stock_index, "_REQUIRED_CSV_PATHS", paths),
        patch.object(refresh_stock_index, "_run"),
        patch.object(refresh_stock_index, "_sync_static_index"),
    ):
        exit_code = refresh_stock_index.main(["--skip-fetch"])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "stock_list_hk.csv" in err and "stock_list_us.csv" in err
