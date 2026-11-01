"""kb frontier: 时间窗口内在动什么。契约见 docs/derived-views.md §3。

与 hubs 一句话分工：hubs 跑全图回答「有哪些主题」（空间视图），frontier 跑窗口内
活跃子图回答「最近在动什么」（时间视图）。两边的「团」不是一回事，不要混叫。

数据源按契约逐信号钉死：新建按 `event_date`，编辑 / 新增边 / 触碰按窗口内 git 史。
git 侧信号是**可注入的输入**，`collect_git_signals` 只是它的生产实现，测试直接
注入字典，不依赖宿主 repo 的 git 史。
"""

import datetime
import subprocess
from pathlib import Path

from .notes import links_in

DEFAULT_WINDOW_DAYS = 14

# 一条笔记在窗口内被 commit 这么多次，标「深挖（或卡住）」。frontier 分不出
# 深挖和卡住（契约 §3.5），两个词都印，判断留给人。
DEEP_TOUCH = 3


def collect_git_signals(notes_dir, cutoff):
    """生产路径：从 git 史取触碰计数与窗口起点的链接集。

    返回 {"touches": {slug: 次数}, "links_before": {slug: 链接列表或 None}}。
    links_before[slug] 是窗口起点时该笔记的链接列表；None = 那时还不存在。
    只对窗口内被 commit 过的文件取 before 版本，没动过的文件长不出新边。
    git 不可用时返回空信号，frontier 退化为纯 event_date 视图，不报错。
    """
    notes_dir = Path(notes_dir)
    empty = {"touches": {}, "links_before": {}}
    since = cutoff.isoformat() + " 00:00:00"
    try:
        log = subprocess.run(
            ["git", "log", "--since", since, "--name-only", "--pretty=format:", "--", "."],
            cwd=str(notes_dir), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return empty
    if log.returncode != 0:
        return empty
    touches = {}
    for line in log.stdout.splitlines():
        name = line.strip()
        if not name.endswith(".md"):
            continue
        slug = Path(name).stem
        touches[slug] = touches.get(slug, 0) + 1
    if not touches:
        return empty

    links_before = {}
    base = subprocess.run(
        ["git", "rev-list", "-1", "--before", since, "HEAD"],
        cwd=str(notes_dir), capture_output=True, text=True, timeout=30,
    )
    base_sha = base.stdout.strip() if base.returncode == 0 else ""
    for slug in sorted(touches):
        if not base_sha:
            links_before[slug] = None
            continue
        show = subprocess.run(
            ["git", "show", "%s:./%s.md" % (base_sha, slug)],
            cwd=str(notes_dir), capture_output=True, text=True, timeout=30,
        )
        links_before[slug] = links_in(show.stdout) if show.returncode == 0 else None
    return {"touches": touches, "links_before": links_before}


def _event_date(note):
    try:
        return datetime.date.fromisoformat(str(note.fm.get("event_date", "")))
    except (ValueError, TypeError):
        return None


def compute(notes, days=DEFAULT_WINDOW_DAYS, today=None, signals=None):
    """纯函数。signals 缺省为空信号 = 纯 event_date 视图（探索/整合仍可判）。"""
    signals = signals or {}
    touches = signals.get("touches", {})
    links_before = signals.get("links_before", {})
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)

    by_slug = {n.slug: n for n in notes}
    known = set(by_slug)

    new_slugs = set()
    for n in notes:
        d = _event_date(n)
        if d is not None and d >= cutoff:
            new_slugs.add(n.slug)
    edited_slugs = {s for s in touches if s in known and s not in new_slugs}
    active = new_slugs | edited_slugs

    # 全图度数，孤儿判定用：窗口内新建且全图零度 = 写了但没挂上任何东西。
    degree = {s: 0 for s in known}
    for n in notes:
        for t in n.links:
            if t in known and t != n.slug:
                degree[n.slug] += 1
                degree[t] += 1

    # 每条活跃笔记的新增边 = 当前链接 − 窗口起点链接。窗口起点不存在
    # （新笔记，或 links_before 记 None）= 全部链接都算新。
    new_edges = set()
    for s in sorted(active):
        before = links_before.get(s)
        if before is None:
            before = []
        before_set = set(before)
        for t in by_slug[s].links:
            if t not in known or t == s or t in before_set:
                continue
            new_edges.add((min(s, t), max(s, t)))

    # 活跃子图：活跃节点 + 两端都活跃的新增边。连旧新边不参与连通，但计分。
    adj = {s: set() for s in active}
    internal_new, old_link = set(), set()
    for a, b in new_edges:
        if a in active and b in active:
            adj[a].add(b)
            adj[b].add(a)
            internal_new.add((a, b))
        else:
            old_link.add((a, b))

    orphans = sorted(s for s in new_slugs if degree.get(s, 0) == 0)
    orphan_set = set(orphans)

    seen, components = set(), []
    for s in sorted(active):
        if s in seen or s in orphan_set:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen and nb not in orphan_set:
                    seen.add(nb)
                    stack.append(nb)
        components.append(sorted(comp))

    clusters = []
    for comp in components:
        cset = set(comp)
        c_internal = sorted(e for e in internal_new if e[0] in cset and e[1] in cset)
        c_old = sorted(e for e in old_link if e[0] in cset or e[1] in cset)
        n_new = sum(1 for s in comp if s in new_slugs)
        total = len(c_internal) + len(c_old)
        if total == 0:
            label = None
        elif 2 * len(c_old) >= total:
            label = "整合"
        else:
            label = "探索"
        deep = sorted(s for s in comp if touches.get(s, 0) >= DEEP_TOUCH)
        ev = {"revealed": 0, "stated": 0, "inferred": 0}
        for s in comp:
            e = by_slug[s].evidence
            if e in ev:
                ev[e] += 1
        clusters.append({
            "members": comp,
            "size": len(comp),
            "new_notes": n_new,
            "edited_notes": len(comp) - n_new,
            "internal_new_edges": len(c_internal),
            "old_link_edges": len(c_old),
            "score": n_new + 2 * len(c_old),
            "label": label,
            "deep_touch": deep,
            "evidence": ev,
        })
    clusters.sort(key=lambda c: (-c["score"], -c["size"], c["members"][0]))

    by_date = {}
    for s in sorted(new_slugs):
        d = _event_date(by_slug[s])
        by_date.setdefault(d.isoformat(), []).append(s)
    timeline = [{"date": d, "new": by_date[d]} for d in sorted(by_date)]

    repeat = sorted(
        ({"slug": s, "count": c} for s, c in touches.items()
         if c >= DEEP_TOUCH and s in known),
        key=lambda r: (-r["count"], r["slug"]),
    )

    return {
        "window_days": days,
        "asof": today.isoformat(),
        "cutoff": cutoff.isoformat(),
        "active_notes": len(active),
        "new_notes": len(new_slugs),
        "edited_notes": len(edited_slugs),
        "timeline": timeline,
        "clusters": clusters,
        "orphans": orphans,
        "repeat_touches": repeat,
    }


def render(result):
    lines = [
        "窗口 %d 天（%s 起，截至 %s）：活跃 %d 条（新建 %d、编辑 %d），活跃团 %d，孤儿 %d"
        % (result["window_days"], result["cutoff"], result["asof"],
           result["active_notes"], result["new_notes"], result["edited_notes"],
           len(result["clusters"]), len(result["orphans"])),
        "",
        "时间线（窗口内新建）：",
    ]
    for row in result["timeline"]:
        lines.append("  %s  %d 条" % (row["date"], len(row["new"])))
    if not result["timeline"]:
        lines.append("  （无）")
    lines.append("")
    for i, c in enumerate(result["clusters"], start=1):
        marks = []
        if c["label"]:
            marks.append(c["label"])
        if c["deep_touch"]:
            marks.append("深挖（或卡住）")
        lines.append(
            "%d. [%s] %d 条（新 %d、编辑 %d），得分 %d ｜ 内部新边 %d，连旧 %d ｜ "
            "证据 revealed %d / stated %d / inferred %d"
            % (i, "，".join(marks) if marks else "无新边", c["size"], c["new_notes"],
               c["edited_notes"], c["score"], c["internal_new_edges"], c["old_link_edges"],
               c["evidence"]["revealed"], c["evidence"]["stated"], c["evidence"]["inferred"])
        )
        head = c["members"][:5]
        more = "" if c["size"] <= 5 else " …共 %d 条" % c["size"]
        lines.append("   %s%s" % (", ".join(head), more))
        if c["deep_touch"]:
            lines.append("   反复触碰：%s" % ", ".join(c["deep_touch"]))
    if not result["clusters"]:
        lines.append("（窗口内没有活跃团）")
    lines.append("")
    lines.append("孤儿（窗口内新建且全图零度）：%s"
                 % (", ".join(result["orphans"]) if result["orphans"] else "无"))
    lines.append("反复触碰（窗口内 commit ≥ %d 次）：%s"
                 % (DEEP_TOUCH,
                    ", ".join("%s×%d" % (r["slug"], r["count"]) for r in result["repeat_touches"])
                    if result["repeat_touches"] else "无"))
    return "\n".join(lines)
