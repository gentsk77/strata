"""kb: single entry point.

每个命令都支持 --json。JSON 输出是给 agent 消费的正式 API，
人类可读的输出是它的皮肤。
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from . import config, doctor, frontier, graph, hubs, ingest, query, related, watch
from .notes import EVIDENCE, load_all, parse_date


def cmd_root(args):
    """打印解析出来的路径：strata 在哪、清单在哪、这个答案哪来的。

    存在的理由是**别处不许再写死这条路径**。skill 与 app 都要知道 strata 的
    checkout 在哪，而「知道」的方式只能有一种，否则迁移一次就要满仓库改路径，
    strata-companion 的旧版把绝对路径写进 skill 正文，自己在文档里把它标成
    「换机器即失效」的已知断点，就是这个失效模式的现成样本。
    """
    payload = args.roots.as_json()
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print("strata:     %s" % payload["strata_root"])
        print("notes:      %s" % payload["notes"])
        print("inbox:      %s" % payload["inbox"])
        print("bench:      %s" % payload["bench_root"])
        print("watchlist:  %s" % payload["watchlist"])
        print("来源:       %s" % payload["source"])
    return 0


def cmd_doctor(args):
    result = doctor.check(args.roots.notes_dir)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(doctor.render(result))
    return 1 if result["violations"] else 0


def _note_date(note):
    try:
        return parse_date(note.fm.get("event_date", ""))
    except (ValueError, TypeError):
        return None


def cmd_list(args):
    """按 evidence 分组列出全部笔记，强度降序。--since / --evidence 是过滤器。"""
    notes, _, _ = load_all(args.roots.notes_dir)

    since = getattr(args, "since", None)
    cutoff = None
    if since:
        try:
            cutoff = parse_date(since)
        except ValueError:
            # 错误也要有 JSON 形态，理由同 cmd_hubs。
            payload = {
                "error": "bad_since",
                "reason": "--since 要绝对日期 YYYY-MM-DD，收到 %r" % since,
            }
            if args.json:
                json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
                sys.stdout.write("\n")
            else:
                print(payload["reason"], file=sys.stderr)
            return 2
        notes = [n for n in notes if (d := _note_date(n)) is not None and d >= cutoff]
    evidence = getattr(args, "evidence", None)
    if evidence:
        notes = [n for n in notes if n.evidence == evidence]

    groups = {}
    for n in notes:
        groups.setdefault(n.evidence or "(无 evidence)", []).append(n)
    if args.json:
        payload = {
            k: [{"slug": n.slug, "title": n.title, "status": n.status,
                 "event_date": n.fm.get("event_date", ""), "links": n.links}
                for n in sorted(v, key=lambda x: x.slug)]
            for k, v in groups.items()
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if not notes:
        filters = []
        if since:
            filters.append("--since %s" % since)
        if evidence:
            filters.append("--evidence %s" % evidence)
        if filters:
            print("0 条命中（%s）" % " ".join(filters))
        else:
            print("库是空的")
        return 0
    order = {v: i for i, v in enumerate(EVIDENCE)}
    for group in sorted(groups, key=lambda g: order.get(g, len(order))):
        # 时间过滤时按日期升序读最顺（「这段时间学了什么」是条时间线），
        # 否则维持 slug 序的稳定快照。
        if cutoff:
            items = sorted(groups[group], key=lambda x: (str(x.fm.get("event_date", "")), x.slug))
        else:
            items = sorted(groups[group], key=lambda x: x.slug)
        print("%s  (%d)" % (group, len(items)))
        for n in items:
            mark = "" if n.status == "settled" else "  [unsettled]"
            if cutoff:
                print("  %s  %s%s" % (n.fm.get("event_date", "????-??-??"), n.title, mark))
            else:
                print("  %s%s" % (n.title, mark))
            print("      %s" % n.slug)
        print("")
    return 0


def cmd_hubs(args):
    """全库有哪些主题，每个从哪条笔记进。派生视图，不落盘。"""
    notes, _, _ = load_all(args.roots.notes_dir)
    try:
        result = hubs.compute(notes, days=args.days)
    except hubs.TooSparse as e:
        # 失败也要有 JSON 形态，否则 agent 拿到的是空 stdout 加一个非零退出码，
        # json.loads 直接抛异常，问不出「为什么没结果」。
        if args.json:
            json.dump(e.as_json(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 1
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif getattr(args, "brief", False):
        print(hubs.render_brief(result))
    else:
        print(hubs.render(result))
    return 0


def cmd_ingest(args):
    """把 watched repo 的新增 md 原文复印进 inbox/。默认来源是 watch 清单。"""
    # workspace（被 watch 的兄弟 repo 从哪解析）跟着 **notes 根** 走，不跟着工具
    # 走：被捞的 repo 与知识库互为兄弟目录，这条关系由知识库的位置定义。
    workspace = doctor._workspace_root(args.roots.notes_dir)
    # 清单跟着**工具**走，inbox 与游标跟着**笔记**走。迁移把这两件事分了家：
    # watchlist.json 是维护者对工具下的一个选择，inbox/ 与 .kb/ 是笔记的副产品。
    config_root = args.roots.config_root
    try:
        if args.repos:
            # 位置参数是一次性覆盖：捞一个不在清单里的 repo 而不改清单。
            # 不带分隔符的一律按 workspace 解析，**不先看当前目录**，先看当前
            # 目录的话，站在任何有同名子目录的地方跑 `kb ingest learning-agent`
            # 会捞到那个同名目录；而游标键是目录名，真 repo 的进度会被一并顶掉，
            # 下次按清单跑就变成全量重扫。
            repos = []
            for spec in args.repos:
                if "/" in spec or "\\" in spec or os.path.splitdrive(spec)[0]:
                    repos.append(Path(spec))          # 明写的路径，照用
                else:
                    repos.append(watch.resolve(workspace, spec))
        else:
            names = watch.load(config_root)
            if not names:
                raise watch.WatchError(
                    "empty_watchlist",
                    "watch 清单是空的。用 `kb watch add <目录名>` 加来源，或直接给 ingest 传路径",
                )
            repos = [watch.resolve(workspace, n) for n in names]
    except watch.WatchError as e:
        # 清单坏了、条目非法、清单为空，都要有结构化形态。裸 traceback 对 agent
        # 等于没有错误信息。
        if args.json:
            json.dump(e.as_json(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 2
    # 够不到的来源逐条报，不整批中止：清单入 git 跨机器共享，另一台机器上少一个
    # 兄弟目录就让整条流水线停摆，与 ingest 自己「跳过不算失败」的姿态相反。
    reachable = [r for r in repos if r.is_dir()]
    unreachable = [str(r) for r in repos if not r.is_dir()]
    if not reachable:
        msg = {"error": "repo_not_found", "missing": unreachable}
        if args.json:
            json.dump(msg, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print("找不到任何 watched repo：%s" % ", ".join(unreachable), file=sys.stderr)
        return 2
    report = ingest.run(reachable, args.roots.inbox_dir, args.roots.cursor_file)
    for miss in unreachable:
        report["repos"].append({"repo": Path(miss).name, "path": miss, "copied": [],
                                "skipped_non_utf8": [], "reset": False, "unchanged": False,
                                "error": "找不到这个目录，已跳过"})
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(ingest.render(report))
    return 1 if any(r["error"] for r in report["repos"]) else 0


def cmd_watch(args):
    """列出或修改 ingest 的来源清单。清单入 git，理由见 kb/watch.py。"""
    # workspace（被 watch 的兄弟 repo 从哪解析）跟着 **notes 根** 走，不跟着工具
    # 走：被捞的 repo 与知识库互为兄弟目录，这条关系由知识库的位置定义。
    workspace = doctor._workspace_root(args.roots.notes_dir)
    # 清单跟着**工具**走，inbox 与游标跟着**笔记**走。迁移把这两件事分了家：
    # watchlist.json 是维护者对工具下的一个选择，inbox/ 与 .kb/ 是笔记的副产品。
    config_root = args.roots.config_root
    words = list(args.words)
    # 认不出的第一个词不许静默退化成「列出」：`kb watch remove x` 是 rm 的自然
    # 手误，退化的话它会打印一份完整清单并退出 0，与真的删成功一模一样。
    if words and words[0] not in ("add", "rm"):
        payload = {"error": "unknown_action",
                   "reason": "不认识的动作 %r。只有 add 和 rm，不给动作就是列出清单" % words[0]}
        if args.json:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(payload["reason"], file=sys.stderr)
        return 2
    args.action = words.pop(0) if words else None
    args.names = words
    try:
        if args.action is None:
            result = watch.status(config_root, workspace)
        elif not args.names:
            raise watch.WatchError("no_names", "`kb watch %s` 要至少一个目录名" % args.action)
        else:
            if args.action == "add":
                changed = watch.add(config_root, workspace, args.names)["added"]
            else:
                changed = watch.remove(config_root, args.names)["removed"]
            # 改动已经落盘。此后再抛错是**报告**失败，不是改动失败，报告失败
            # 不许把退出码变成 2，否则调用方会以为清单没动过。
            try:
                result = watch.status(config_root, workspace)
            except watch.WatchError as e:
                print("改动已生效，但列清单时出错：%s" % e, file=sys.stderr)
                result = {"watched": [], "count": 0, "listing_error": e.as_json()}
            # 调用方要能分辨「删掉了 beta」和「什么都没改」，光看清单分不出来。
            result[args.action + "ed" if args.action == "add" else "removed"] = changed
    except watch.WatchError as e:
        if args.json:
            json.dump(e.as_json(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(watch.render(result))
    return 0


def cmd_frontier(args):
    """时间窗口内在动什么。git 侧信号取自生产路径，取不到就退化为纯 event_date 视图。"""
    notes, _, _ = load_all(args.roots.notes_dir)
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    signals = frontier.collect_git_signals(args.roots.notes_dir, cutoff)
    result = frontier.compute(notes, days=args.days, today=today, signals=signals)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(frontier.render(result))
    return 0


def cmd_query(args):
    """按一段文字全文检索。0 命中退出 1（grep 惯例），空检索词退出 2。"""
    notes, _, _ = load_all(args.roots.notes_dir)
    try:
        result = query.compute(notes, args.text)
    except query.EmptyQuery as e:
        # 错误也要有 JSON 形态，理由同 cmd_hubs。
        if args.json:
            json.dump(e.as_json(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(query.render(result))
    return 0 if result["count"] else 1


def cmd_related(args):
    """从一个 slug 出发沿链接走 N 跳。起点不存在或 hops 非法退出 2。"""
    notes, _, _ = load_all(args.roots.notes_dir)
    try:
        result = related.compute(notes, args.slug, hops=args.hops)
    except (related.UnknownSlug, ValueError) as e:
        if args.json:
            payload = e.as_json() if hasattr(e, "as_json") else {"error": "bad_hops", "reason": str(e)}
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(related.render(result))
    return 0


def cmd_graph(args):
    """整库的图，一次给全：节点、边、每条边的理由句。

    与 `kb related` 的分工是**范围**，不是内容：related 从一个 slug 出发看邻域，
    graph 不设起点、把整张图一次交出去。审理屏的图栏要的是后者（D16 第 1 条），
    靠 related 拼的话得按每条笔记各跑一次，而且拼出来的边会互相打架。
    两者共用 kb/graph.py 里那份邻接与理由句，所以不会给出两张不同的图。
    """
    notes, _, _ = load_all(args.roots.notes_dir)
    result = graph.compute(notes)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(graph.render(result))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="kb", description="strata knowledge base")
    p.add_argument(
        "--root",
        default=None,
        help="把这个目录当成自包含的 kb 工作区（notes/ 与 watchlist.json 都取自它）。"
             "不给就按 %s、%s、兄弟目录默认值的顺序解析，见 `kb root`"
             % (config.ENV_VAR, config.CONFIG_NAME),
    )
    p.add_argument("--json", action="store_true", help="机器可读输出")
    sub = p.add_subparsers(dest="command", required=True)

    # --root/--json 在子命令上再挂一份：`kb hubs --json` 是任何人的第一直觉，
    # 而只认 `kb --json hubs` 会让「每个命令都支持 --json」这条契约名存实亡。
    # default=SUPPRESS 是必须的：argparse 的子解析器会用自己的默认值覆盖顶层
    # 已经解析好的同名属性，所以不加它，`kb --root tests/fixtures doctor` 会被
    # 子解析器把 root 打回 None，静默滑回真 repo 去跑。
    def add(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS, help="机器可读输出"
        )
        return sp

    rt = add("root", help="打印解析出来的路径：strata 在哪、清单在哪、答案哪来的")
    rt.set_defaults(func=cmd_root)

    d = add("doctor", help="检查全部笔记不变量")
    d.set_defaults(func=cmd_doctor)

    l = add("list", help="按 evidence 分组列出笔记")
    l.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="只列 event_date 在这天及之后的笔记，组内按日期升序")
    l.add_argument("--evidence", default=None, choices=list(EVIDENCE),
                   help="只列这个证据档")
    l.set_defaults(func=cmd_list)

    q = add("query", help="按一段文字全文检索（标题、slug、正文）")
    q.add_argument("text", help="要找的文字。整段作为一个子串匹配，大小写不敏感")
    q.set_defaults(func=cmd_query)

    r = add("related", help="从一个 slug 出发看它周围：节点、边、每条边的理由句")
    r.add_argument("slug", help="起点笔记的 slug")
    r.add_argument("--hops", type=int, default=1, help="沿链接走几跳（默认 1）")
    r.set_defaults(func=cmd_related)

    gr = add("graph", help="整库的图：全部节点、全部边、每条边的理由句")
    gr.set_defaults(func=cmd_graph)

    h = add("hubs", help="全库有哪些主题，每个从哪条笔记进")
    h.add_argument(
        "--days",
        type=int,
        default=hubs.DEFAULT_WINDOW_DAYS,
        help="「最近有没有新成员」的窗口天数（默认 %d）" % hubs.DEFAULT_WINDOW_DAYS,
    )
    h.add_argument("--brief", action="store_true",
                   help="每主题一行：等级、条数、窗口新增、入口。只影响人读输出")
    h.set_defaults(func=cmd_hubs)

    g = add("ingest", help="把 watched repo 的新增 md 原文复印进 inbox/（复印机，不产生知识）")
    g.add_argument("repos", nargs="*",
                   help="要捞的目录名或路径（不给就用 watch 清单）")
    g.set_defaults(func=cmd_ingest)

    w = add("watch", help="维护 ingest 的来源清单：不带参数是列出，add / rm 改清单")
    # 一个 nargs="*" 收全部位置参数，动作从第一个词自己切出来。写成 nargs="?"
    # 加 nargs="*" 的话，argparse 在中间夹一个可选参数之后就填不回第二个位置
    # 参数，于是 `kb watch add --json alpha` 报 unrecognized arguments。
    w.add_argument("words", nargs="*",
                   help="[add|rm] 后跟 workspace 下的目录名（例：learning-agent），不是路径")
    w.set_defaults(func=cmd_watch)

    f = add("frontier", help="时间窗口内在动什么：时间线、活跃团、孤儿、反复触碰")
    f.add_argument(
        "--days",
        type=int,
        default=frontier.DEFAULT_WINDOW_DAYS,
        help="窗口天数（默认 %d）" % frontier.DEFAULT_WINDOW_DAYS,
    )
    f.set_defaults(func=cmd_frontier)

    return p


def main(argv=None):
    # Windows 控制台默认不是 UTF-8，笔记标题是中文，不强制就是一屏乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    # argparse 的 nargs="*" 位置参数在中间夹一个可选参数之后就填不回来，于是
    # `kb watch add --json alpha` 会把 alpha 判成 unrecognized。这不是用法错误，
    # 而 `--json` 下报用法错误意味着 agent 拿到的是空 stdout。所以收下剩余的
    # **非** flag 词补回 watch 的位置参数；真正认不出的 flag 仍然照常报错。
    args, extra = parser.parse_known_args(argv)
    if extra:
        if getattr(args, "command", None) == "watch" and all(not e.startswith("-") for e in extra):
            args.words = list(args.words) + extra
        else:
            parser.error("unrecognized arguments: %s" % " ".join(extra))
    # 路径解析集中在这里跑一次，不散进八个 cmd_*：每个命令自己 try/except 的话，
    # 「配置坏了」这条错误会有八份措辞，而它恰恰是搬家之后最常见的一条。
    try:
        args.roots = config.resolve(getattr(args, "root", None))
    except config.ConfigError as e:
        if args.json:
            json.dump(e.as_json(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(str(e), file=sys.stderr)
        return 2
    return args.func(args)
