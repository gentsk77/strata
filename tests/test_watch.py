"""kb watch 的回归测试。零依赖：python tests/test_watch.py

workspace 与 watched repo 都在临时目录里现造，跑完删。断言对着 kb/watch.py
钉死的行为：入 git 的清单文件、workspace 相对名、验存在 / 验 git 根、幂等、
坏文件当空清单、ingest 空清单时的退出码。
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import cli, watch

# --------------------------------------------------------------------------
# 极简断言框架（与 test_ingest.py 同款）
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
# 夹具：一个 workspace，里面若干 git repo，外加一个假的 strata
# --------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git"] + list(args), cwd=str(repo),
                          capture_output=True, text=True, timeout=30)


def _rmtree(path):
    """Windows 上 .git/objects 是只读的，直接 rmtree 会 PermissionError。"""
    def _force(func, target, _exc):
        import os
        import stat
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onerror=_force)


def make_workspace(tmp, repo_names, plain_dirs=()):
    """造 workspace/：每个 repo_names 是 git repo，plain_dirs 是普通目录。

    strata 自己也在 workspace 下，且带 notes/，watch 解析 workspace 的方式
    与 doctor 的 artifact 指针同款（notes/ 往上两级），夹具必须复刻这个布局。
    """
    ws = Path(tmp) / "workspace"
    strata = ws / "strata"
    (strata / "notes").mkdir(parents=True)
    for name in repo_names:
        r = ws / name
        r.mkdir(parents=True)
        (r / "seed.md").write_text("seed\n", encoding="utf-8")
        _git(r, "init", "-q")
        _git(r, "config", "user.email", "t@example.com")
        _git(r, "config", "user.name", "t")
        _git(r, "add", ".")
        _git(r, "commit", "-q", "-m", "seed")
    for name in plain_dirs:
        (ws / name).mkdir(parents=True)
    return ws, strata


def run_cli(argv):
    """跑一次 CLI，返回 (退出码, stdout)。--root 指到夹具的 strata。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli.main(argv)
        except SystemExit as e:       # argparse 的用法错误
            code = e.code if isinstance(e.code, int) else 2
    return code, buf.getvalue()


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def test_add_and_load(tmp):
    case("add 落盘并能读回")
    ws, strata = make_workspace(tmp, ["alpha", "beta"])
    watch.add(strata, ws, ["alpha"])
    eq(watch.load(strata), ["alpha"], "加一条之后清单里就是它")

    watch.add(strata, ws, ["beta"])
    eq(watch.load(strata), ["alpha", "beta"], "第二条追加，不覆盖第一条")

    case("清单文件住 repo 根，是 JSON，字段叫 watched")
    f = strata / "watchlist.json"
    ok(f.exists(), "watchlist.json 在 repo 根")
    data = json.loads(f.read_text(encoding="utf-8"))
    eq(data, {"watched": ["alpha", "beta"]}, "文件内容是 {watched: [名字]}")


def test_add_is_idempotent(tmp):
    case("重复 add 是无操作，不报错不产生重复条目")
    ws, strata = make_workspace(tmp, ["alpha"])
    watch.add(strata, ws, ["alpha"])
    result = watch.add(strata, ws, ["alpha"])
    eq(result["added"], [], "第二次 add 什么都没加")
    eq(watch.load(strata), ["alpha"], "清单里仍然只有一条")


def test_add_rejects_bad_targets(tmp):
    case("加不存在的目录：报 not_found，清单不动")
    ws, strata = make_workspace(tmp, ["alpha"], plain_dirs=["notrepo"])
    watch.add(strata, ws, ["alpha"])
    try:
        watch.add(strata, ws, ["ghost"])
        ok(False, "应该抛 WatchError")
    except watch.WatchError as e:
        eq(e.kind, "not_found", "错误类型是 not_found")
    eq(watch.load(strata), ["alpha"], "失败之后清单一字未动")

    case("加不是 git repo 的目录：报 not_a_git_root")
    try:
        watch.add(strata, ws, ["notrepo"])
        ok(False, "应该抛 WatchError")
    except watch.WatchError as e:
        eq(e.kind, "not_a_git_root", "错误类型是 not_a_git_root")
    eq(watch.load(strata), ["alpha"], "失败之后清单一字未动")

    case("整批里有一条不合法，整批都不落盘")
    # 第一个必须是**合法且尚未在清单里**的条目，否则第一条就抛错，
    # 「逐条保存」的实现也能过这个用例，等于什么都没测。
    ws2 = ws / "beta"
    ws2.mkdir()
    (ws2 / "seed.md").write_text("seed\n", encoding="utf-8")
    _git(ws2, "init", "-q")
    _git(ws2, "config", "user.email", "t@example.com")
    _git(ws2, "config", "user.name", "t")
    _git(ws2, "add", ".")
    _git(ws2, "commit", "-q", "-m", "seed")
    try:
        watch.add(strata, ws, ["beta", "ghost"])
        ok(False, "应该抛 WatchError")
    except watch.WatchError:
        pass
    eq(watch.load(strata), ["alpha"], "合法的 beta 也没被写进去")


def test_add_rejects_paths(tmp):
    case("条目必须是目录名，不是路径")
    ws, strata = make_workspace(tmp, ["alpha"])
    for bad in ["../alpha", "sub/alpha", "sub\\alpha"]:
        try:
            watch.add(strata, ws, [bad])
            ok(False, "%s 应该被拒" % bad)
        except watch.WatchError as e:
            eq(e.kind, "not_a_name", "%s 报 not_a_name" % bad)


def test_remove(tmp):
    case("rm 删掉指定条目，留下其余")
    ws, strata = make_workspace(tmp, ["alpha", "beta"])
    watch.add(strata, ws, ["alpha", "beta"])
    watch.remove(strata, ["alpha"])
    eq(watch.load(strata), ["beta"], "只剩没被删的那条")

    case("rm 不在清单里的条目：报 not_watched 并带上现有清单")
    try:
        watch.remove(strata, ["ghost"])
        ok(False, "应该抛 WatchError")
    except watch.WatchError as e:
        eq(e.kind, "not_watched", "错误类型是 not_watched")
        eq(e.as_json()["watched"], ["beta"], "错误里带现有清单，好让人看出打错了什么")
    eq(watch.load(strata), ["beta"], "失败之后清单一字未动")


def test_broken_file_is_not_empty_list(tmp):
    case("清单坏了要报错，不能当空清单")
    ws, strata = make_workspace(tmp, ["alpha"])
    for bad in ["{ not json", "[]", '{"watched": "alpha"}', '{"other": ["alpha"]}',
                '{"watched": ["sub/beta"]}', '{"watched": [1]}']:
        (strata / "watchlist.json").write_text(bad, encoding="utf-8")
        try:
            watch.load(strata)
            ok(False, "坏内容 %r 应该抛 WatchError" % bad[:22])
        except watch.WatchError:
            pass

    case("坏清单上跑 add 不许把幸存条目冲掉")
    # 这是坏清单当空清单时最贵的后果：五条的清单变成一条，还静默退出 0。
    (strata / "watchlist.json").write_text("{ truncated", encoding="utf-8")
    try:
        watch.add(strata, ws, ["alpha"])
        ok(False, "应该抛 WatchError 而不是覆盖")
    except watch.WatchError:
        pass
    eq((strata / "watchlist.json").read_text(encoding="utf-8"), "{ truncated",
       "原文件一字未动，还能从 git 捞回来")

    case("只有缺文件才算空清单")
    (strata / "watchlist.json").unlink()
    eq(watch.load(strata), [], "没有文件就是空清单")


def test_bare_name_never_binds_to_cwd(tmp):
    case("ingest 的目录名按 workspace 解析，不看当前目录")
    # 当前目录下有个同名 repo 时，若先看当前目录就会捞错 repo，而游标键是目录名，
    # 真 repo 的进度会被一起顶掉。
    ws, strata = make_workspace(tmp, ["alpha"])
    (ws / "alpha" / "f.md").write_text("REAL\n", encoding="utf-8")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "real")

    decoy_parent = Path(tmp) / "elsewhere"
    decoy = decoy_parent / "alpha"
    decoy.mkdir(parents=True)
    (decoy / "f.md").write_text("DECOY\n", encoding="utf-8")
    _git(decoy, "init", "-q")
    _git(decoy, "config", "user.email", "t@example.com")
    _git(decoy, "config", "user.name", "t")
    _git(decoy, "add", ".")
    _git(decoy, "commit", "-q", "-m", "decoy")

    import os
    here = os.getcwd()
    try:
        os.chdir(str(decoy_parent))
        run_cli(["--root", str(strata), "ingest", "alpha"])
    finally:
        os.chdir(here)
    body = "".join(p.read_text(encoding="utf-8") for p in (strata / "inbox").glob("*"))
    ok("REAL" in body and "DECOY" not in body, "捞到的是 workspace 下那个 alpha")


def test_dot_and_drive_names_rejected(tmp):
    case(". / .. / 盘符都不是合法条目")
    ws, strata = make_workspace(tmp, ["alpha"])
    for bad in [".", "..", "D:"]:
        try:
            watch.add(strata, ws, [bad])
            ok(False, "%r 应该被拒" % bad)
        except watch.WatchError as e:
            eq(e.kind, "not_a_name", "%r 报 not_a_name" % bad)


def test_one_missing_repo_does_not_abort_batch(tmp):
    case("清单里有一个够不到的 repo，其余照捞")
    ws, strata = make_workspace(tmp, ["alpha"])
    (ws / "alpha" / "doc.md").write_text("# 原料\n", encoding="utf-8")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "doc")
    watch.save(strata, ["alpha", "gone"])

    code, _ = run_cli(["--root", str(strata), "ingest"])
    copied = [p.name for p in (strata / "inbox").glob("*")]
    ok(any("alpha--doc.md" in c for c in copied), "够得到的那个照样被捞：%s" % copied)
    eq(code, 1, "有跳过的来源，退出码 1（不是 2 的整批失败）")

    case("一个都够不到才算整批失败")
    ws2, strata2 = make_workspace(tmp + "/allgone", ["alpha"])
    watch.save(strata2, ["gone"])
    code, _ = run_cli(["--root", str(strata2), "ingest"])
    eq(code, 2, "全都够不到，退出码 2")


def test_rm_error_names_the_survivors(tmp):
    case("删错名字时，人读的那句话里要有现有条目")
    ws, strata = make_workspace(tmp, ["alpha"])
    watch.add(strata, ws, ["alpha"])
    try:
        watch.remove(strata, ["ghost"])
        ok(False, "应该抛 WatchError")
    except watch.WatchError as e:
        ok("alpha" in str(e), "错误正文里带现有条目，不是只在 JSON 里：%s" % e)


def test_json_flag_position(tmp):
    case("--json 放在哪个位置都能用")
    ws, strata = make_workspace(tmp, ["alpha"])
    for argv in [["watch", "add", "--json", "alpha"],
                 ["watch", "--json", "add", "alpha"],
                 ["watch", "add", "alpha", "--json"],
                 ["--json", "watch", "add", "alpha"]]:
        code, out = run_cli(["--root", str(strata)] + argv)
        eq(code, 0, "%s 退出 0" % " ".join(argv))
        try:
            json.loads(out)
        except ValueError:
            ok(False, "%s 的输出不是 JSON：%r" % (" ".join(argv), out[:40]))


def test_status_reports_reachability(tmp):
    case("status 报每条现在还在不在")
    ws, strata = make_workspace(tmp, ["alpha", "beta"])
    watch.add(strata, ws, ["alpha", "beta"])
    _rmtree(ws / "beta")
    st = watch.status(strata, ws)
    by_name = {e["name"]: e for e in st["watched"]}
    eq(by_name["alpha"]["exists"], True, "还在的报 exists=True")
    eq(by_name["beta"]["exists"], False, "被删掉的报 exists=False")
    ok("[找不到]" in watch.render(st), "人读输出把找不到的标出来")


def test_ingest_reads_watchlist(tmp):
    case("ingest 不带参数时读清单")
    ws, strata = make_workspace(tmp, ["alpha"])
    (ws / "alpha" / "doc.md").write_text("# 一份原料\n", encoding="utf-8")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "add doc")
    watch.add(strata, ws, ["alpha"])

    code, _ = run_cli(["--root", str(strata), "ingest"])
    eq(code, 0, "跑通，退出码 0")
    copied = sorted(p.name for p in (strata / "inbox").glob("*"))
    ok(any("alpha--doc.md" in c for c in copied), "清单里的 repo 被捞了：%s" % copied)

    case("空清单时 ingest 退出 2 并说清怎么办")
    ws2, strata2 = make_workspace(tmp + "/second", ["alpha"])
    code, _ = run_cli(["--root", str(strata2), "ingest"])
    eq(code, 2, "空清单退出码 2")

    case("ingest 的位置参数收目录名，与 `watch add` 收的东西一致")
    ws3, strata3 = make_workspace(tmp + "/third", ["alpha"])
    (ws3 / "alpha" / "doc.md").write_text("# 一份原料\n", encoding="utf-8")
    _git(ws3 / "alpha", "add", ".")
    _git(ws3 / "alpha", "commit", "-q", "-m", "add doc")
    # 清单是空的：这一跑证明位置参数确实绕过清单，且光秃秃的名字能解析。
    code, _ = run_cli(["--root", str(strata3), "ingest", "alpha"])
    eq(code, 0, "`kb ingest <目录名>` 跑通，不需要先加进清单")
    ok((strata3 / "inbox").is_dir(), "inbox 被写出来了")
    eq(watch.load(strata3), [], "一次性覆盖不改清单")


def test_cli_list_and_errors(tmp):
    case("`kb watch` 不带参数是列出")
    ws, strata = make_workspace(tmp, ["alpha"])
    code, out = run_cli(["--root", str(strata), "watch"])
    eq(code, 0, "空清单也是 0，列出不是错误")
    ok("空的" in out, "空清单时告诉人怎么加：%r" % out)

    run_cli(["--root", str(strata), "watch", "add", "alpha"])
    code, out = run_cli(["--root", str(strata), "watch"])
    ok("alpha" in out, "加完之后列得出来")

    case("`kb watch add` 不给名字：退出 2")
    code, _ = run_cli(["--root", str(strata), "watch", "add"])
    eq(code, 2, "缺参数退出码 2")

    case("--json 下错误也有结构化形态")
    code, out = run_cli(["--root", str(strata), "watch", "--json", "rm", "ghost"])
    eq(code, 2, "删不存在的条目退出码 2")
    payload = json.loads(out)
    eq(payload["error"], "not_watched", "JSON 里带 error 字段")


def test_unknown_verb_is_an_error(tmp):
    case("认不出的动作不许静默退化成「列出」")
    # `remove` 是 `rm` 的自然手误。退化的话它会打印完整清单并退出 0，
    # 与真的删成功一模一样，调用方分不出来。
    ws, strata = make_workspace(tmp, ["alpha", "beta"])
    watch.add(strata, ws, ["alpha", "beta"])
    for verb in ["remove", "delete", "add-repo"]:
        code, out = run_cli(["--root", str(strata), "watch", verb, "alpha"])
        eq(code, 2, "`watch %s` 退出 2" % verb)
        eq(watch.load(strata), ["alpha", "beta"], "`watch %s` 之后清单没动" % verb)

    case("成功输出要说清改了什么")
    code, out = run_cli(["--root", str(strata), "watch", "--json", "rm", "alpha"])
    eq(code, 0, "删成功退出 0")
    eq(json.loads(out).get("removed"), ["alpha"], "载荷里带 removed，不能只回一份清单")


def test_trailing_space_name_rejected(tmp):
    case("首尾带空白的条目要拒")
    # Windows 会把结尾空格吃掉，于是 "alpha " 与 "alpha" 指同一个目录却是两个
    # 条目：同一份原料复印两遍，游标长出两个键。
    ws, strata = make_workspace(tmp, ["alpha"])
    watch.add(strata, ws, ["alpha"])
    try:
        watch.add(strata, ws, ["alpha "])
        ok(False, "'alpha ' 应该被拒")
    except watch.WatchError as e:
        eq(e.kind, "not_a_name", "报 not_a_name")
    eq(watch.load(strata), ["alpha"], "清单里仍然只有一条")


def test_unwritable_watchlist_is_a_watcherror(tmp):
    case("清单写不进去要包成 WatchError，且不留 .tmp 残渣")
    import stat
    ws, strata = make_workspace(tmp, ["alpha"])
    watch.add(strata, ws, ["alpha"])
    f = strata / "watchlist.json"
    f.chmod(stat.S_IREAD)
    try:
        watch.save(strata, ["alpha", "beta"])
        ok(False, "只读文件上 save 应该抛 WatchError")
    except watch.WatchError as e:
        eq(e.kind, "unwritable_watchlist", "报 unwritable_watchlist")
    except OSError:
        ok(False, "抛的是裸 OSError，会绕过调用方的 except 变成 traceback")
    finally:
        f.chmod(stat.S_IWRITE | stat.S_IREAD)
    ok(not (strata / "watchlist.json.tmp").exists(), "临时文件已收干净")


# --------------------------------------------------------------------------

def main():
    cases = [
        test_add_and_load,
        test_add_is_idempotent,
        test_add_rejects_bad_targets,
        test_add_rejects_paths,
        test_remove,
        test_broken_file_is_not_empty_list,
        test_bare_name_never_binds_to_cwd,
        test_dot_and_drive_names_rejected,
        test_one_missing_repo_does_not_abort_batch,
        test_rm_error_names_the_survivors,
        test_json_flag_position,
        test_unknown_verb_is_an_error,
        test_trailing_space_name_rejected,
        test_unwritable_watchlist_is_a_watcherror,
        test_status_reports_reachability,
        test_ingest_reads_watchlist,
        test_cli_list_and_errors,
    ]
    for fn in cases:
        tmp = tempfile.mkdtemp(prefix="kb-watch-test-")
        try:
            fn(tmp)
        finally:
            _rmtree(Path(tmp))

    if _failures:
        print("失败 %d 项（共 %d 个断言）：" % (len(_failures), _checks))
        print("\n".join(_failures))
        return 1
    print("全过：%d 个断言" % _checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
