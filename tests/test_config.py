"""kb config 的回归测试。零依赖：python tests/test_config.py

2026-08-13 迁移新增：工具搬进 strata-bench 之后，「知识库在哪」从「向上找
notes/」变成一条要显式解析的配置。本文件钉死那条解析链的每一环，尤其是**失败要
响**这一条，迁移前的兜底是「找不到就用 cwd」，那会让 doctor 在一个没有笔记的
目录里打印「检查了 0 条笔记，全绿」并退出 0。

夹具全在临时目录里现造，跑完删；不碰真的 strata，也不读真的 strata.config.json
（每个用例都把 kb.config 的两个「我在哪」函数打桩掉）。
"""

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import cli, config

# --------------------------------------------------------------------------
# 极简断言框架（与 test_watch.py 同款）
# --------------------------------------------------------------------------

_checks = 0
_failures = []
_current = ""


def _record(msg):
    frame = sys._getframe(2)
    _failures.append(
        "  [%s] %s:%d\n    %s" % (_current, Path(frame.f_code.co_filename).name, frame.f_lineno, msg)
    )


def ok(cond, what):
    global _checks
    _checks += 1
    if not cond:
        _record(what)


def eq(got, want, what):
    global _checks
    _checks += 1
    if got != want:
        _record("%s\n    期望 %r，实得 %r" % (what, want, got))


def case(name):
    global _current
    _current = name


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def make_layout(tmp, with_notes=True):
    """造 workspace/{strata,strata-bench}，返回 (workspace, strata, bench)。

    复刻真实布局：两个 repo 互为兄弟目录，strata 下有 notes/。
    """
    ws = Path(tmp) / "workspace"
    strata = ws / "strata"
    bench = ws / "strata-bench"
    bench.mkdir(parents=True)
    if with_notes:
        (strata / "notes").mkdir(parents=True)
    else:
        strata.mkdir(parents=True)
    return ws, strata, bench


class _Stub:
    """把 config 的两个「我在哪」函数指到夹具上，退出时还原。

    必须连 main_checkout 一起打桩：真跑的时候它会去问 git，而夹具目录不在任何
    repo 里，问出来的答案是夹具自己，能用，但用例就变成在测 git 的行为而不是
    在测解析顺序。
    """

    def __init__(self, bench):
        self.bench = Path(bench)

    def __enter__(self):
        self._bench_root = config.bench_root
        self._main_checkout = config.main_checkout
        config.bench_root = lambda: self.bench
        config.main_checkout = lambda start=None: self.bench
        self._env = os.environ.pop(config.ENV_VAR, None)
        return self

    def __exit__(self, *exc):
        config.bench_root = self._bench_root
        config.main_checkout = self._main_checkout
        if self._env is None:
            os.environ.pop(config.ENV_VAR, None)
        else:
            os.environ[config.ENV_VAR] = self._env
        return False


def write_config(bench, value):
    (Path(bench) / config.CONFIG_NAME).write_text(
        json.dumps({config.CONFIG_KEY: value}, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def run_cli(argv):
    """跑一次 CLI，返回 (退出码, stdout)。与 test_watch.run_cli 同款。"""
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
    return code, buf.getvalue()


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_precedence(tmp):
    case("解析顺序：--root > 环境变量 > 配置文件 > 兄弟目录默认值")
    ws, strata, bench = make_layout(tmp)
    other = ws / "other-kb"
    (other / "notes").mkdir(parents=True)

    with _Stub(bench):
        r = config.resolve()
        eq(r.notes_root, strata.resolve(), "什么都没有时落到兄弟目录默认值")
        eq(r.source, "兄弟目录默认值", "来源标成兄弟目录默认值")

        write_config(bench, str(other))
        r = config.resolve()
        eq(r.notes_root, other.resolve(), "配置文件盖过兄弟目录默认值")
        eq(r.source, config.CONFIG_NAME, "来源标成配置文件")

        os.environ[config.ENV_VAR] = str(strata)
        r = config.resolve()
        eq(r.notes_root, strata.resolve(), "环境变量盖过配置文件")
        eq(r.source, config.ENV_VAR, "来源标成环境变量")

        r = config.resolve(str(other))
        eq(r.notes_root, other, "--root 盖过一切")
        eq(r.source, "--root", "来源标成 --root")


def test_root_flag_is_self_contained(tmp):
    case("--root 同时决定 notes/ 与 watchlist.json，即迁移前的原样语义")
    ws, strata, bench = make_layout(tmp)
    with _Stub(bench):
        r = config.resolve(str(strata))
        eq(r.notes_root, strata, "notes 根是 --root")
        eq(r.config_root, strata, "清单根也是 --root，不是 bench")
        eq(r.watchlist_file, strata / "watchlist.json", "清单文件落在 --root 下")

    case("不给 --root 时两个根分家：清单跟工具走，笔记跟配置走")
    with _Stub(bench):
        write_config(bench, str(strata))
        r = config.resolve()
        eq(r.notes_root, strata.resolve(), "notes 根是配置出来的 strata")
        eq(r.config_root, bench, "清单根是 bench")
        eq(r.watchlist_file, bench / "watchlist.json", "清单文件落在 bench 下")
        eq(r.inbox_dir, strata.resolve() / "inbox", "inbox 跟着笔记走，不跟工具走")
        eq(r.cursor_file, strata.resolve() / ".kb" / "ingest-cursor.json", "游标跟着笔记走")


def test_relative_config_resolves_against_bench(tmp):
    case("配置里的相对路径按 bench 主 checkout 解析，不按 cwd")
    ws, strata, bench = make_layout(tmp)
    with _Stub(bench):
        write_config(bench, "../strata")
        here = os.getcwd()
        try:
            # 站到一个完全无关的目录里再解析：按 cwd 解析的话这里就会解错。
            os.chdir(tempfile.gettempdir())
            r = config.resolve()
        finally:
            os.chdir(here)
        eq(r.notes_root, strata.resolve(), "../strata 解析到兄弟目录，与 cwd 无关")


def test_missing_notes_dir_is_loud(tmp):
    case("配置指到一个没有 notes/ 的目录：报错，不静默给空结果")
    ws, strata, bench = make_layout(tmp, with_notes=False)
    with _Stub(bench):
        write_config(bench, str(strata))
        try:
            config.resolve()
            ok(False, "应该抛 ConfigError")
        except config.ConfigError as e:
            eq(e.kind, "notes_dir_missing", "错误类型是 notes_dir_missing")
            ok(str(strata) in e.reason, "错误里带上了它到底看的哪个目录")


def test_nothing_configured_is_loud(tmp):
    case("什么都没配且没有兄弟目录：报错并列出试过哪些地方")
    lonely = Path(tmp) / "lonely" / "bench"
    lonely.mkdir(parents=True)
    with _Stub(lonely):
        try:
            config.resolve()
            ok(False, "应该抛 ConfigError")
        except config.ConfigError as e:
            eq(e.kind, "strata_root_not_found", "错误类型是 strata_root_not_found")
            eq(len(e.extra["tried"]), 3, "三条来源都记进了 tried")
            ok(config.ENV_VAR in e.reason, "修法里点名了环境变量")
            ok(config.CONFIG_NAME in e.reason, "修法里点名了配置文件")


def test_cli_exits_2_and_emits_json_on_config_error(tmp):
    case("解析失败时 CLI 退出 2，--json 下给结构化错误而不是空 stdout")
    lonely = Path(tmp) / "lonely" / "bench"
    lonely.mkdir(parents=True)
    with _Stub(lonely):
        code, out = run_cli(["doctor", "--json"])
        eq(code, 2, "退出码是 2")
        try:
            payload = json.loads(out)
        except ValueError:
            _record("--json 下的输出不是 JSON：%r" % out[:80])
            payload = {}
        eq(payload.get("error"), "strata_root_not_found", "JSON 里带错误类型")

        code, out = run_cli(["doctor"])
        eq(code, 2, "不加 --json 也退出 2")
        eq(out, "", "人读形态把错误写 stderr，stdout 保持空")


def test_corrupt_config_is_not_treated_as_absent(tmp):
    case("配置文件读不懂：报 corrupt_config，不静默滑到默认值")
    ws, strata, bench = make_layout(tmp)
    with _Stub(bench):
        (bench / config.CONFIG_NAME).write_text("{ truncated", encoding="utf-8")
        try:
            config.resolve()
            ok(False, "应该抛 ConfigError")
        except config.ConfigError as e:
            eq(e.kind, "corrupt_config", "错误类型是 corrupt_config")

    case("字段缺失不算坏文件，只算没配（继续往下找）")
    with _Stub(bench):
        (bench / config.CONFIG_NAME).write_text('{"somethingElse": 1}', encoding="utf-8")
        r = config.resolve()
        eq(r.source, "兄弟目录默认值", "没有 strataRoot 字段就继续找下一档")


def test_bom_config_is_accepted(tmp):
    case("带 BOM 的配置文件照样读得懂（Windows 上一堆写法默认加 BOM）")
    ws, strata, bench = make_layout(tmp)
    with _Stub(bench):
        (bench / config.CONFIG_NAME).write_text(
            json.dumps({config.CONFIG_KEY: str(strata)}), encoding="utf-8-sig"
        )
        r = config.resolve()
        eq(r.notes_root, strata.resolve(), "BOM 被吃掉，路径照常解析")


def test_no_silent_cwd_fallback(tmp):
    case("站在一个有 notes/ 的目录里也不会被当成知识库（旧的向上找已删除）")
    ws, strata, bench = make_layout(tmp)
    decoy = Path(tmp) / "decoy"
    (decoy / "notes").mkdir(parents=True)
    lonely = Path(tmp) / "lonely" / "bench"
    lonely.mkdir(parents=True)
    with _Stub(lonely):
        here = os.getcwd()
        try:
            os.chdir(decoy)
            try:
                config.resolve()
                ok(False, "cwd 下有 notes/ 也不该被采纳")
            except config.ConfigError as e:
                eq(e.kind, "strata_root_not_found", "仍然报找不到")
        finally:
            os.chdir(here)
    ok(not hasattr(cli, "repo_root"), "旧的 cli.repo_root 已删除，没有第二条解析路径")


def main():
    cases = [
        test_precedence,
        test_root_flag_is_self_contained,
        test_relative_config_resolves_against_bench,
        test_missing_notes_dir_is_loud,
        test_nothing_configured_is_loud,
        test_cli_exits_2_and_emits_json_on_config_error,
        test_corrupt_config_is_not_treated_as_absent,
        test_bom_config_is_accepted,
        test_no_silent_cwd_fallback,
    ]
    for fn in cases:
        tmp = tempfile.mkdtemp(prefix="kb-config-test-")
        try:
            fn(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if _failures:
        print("失败 %d 项（共 %d 个断言）：" % (len(_failures), _checks))
        print("\n".join(_failures))
        return 1
    print("全过：%d 个断言" % _checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
