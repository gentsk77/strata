"""kb hubs: 全库有哪些主题，每个主题从哪条笔记进。

算法契约见 docs/derived-views.md §2。要点：

  - 主题 = 链接图上的一个社区。跑在**全图**上，与 `frontier` 的「活跃团」
    不是一回事，不要混叫 cluster。
  - 入口 = 社区内入链最多的那条笔记。别人最常指向它，说明它是这个主题的地基。
  - 主题名直接用入口笔记的 title。论断式标题在这里回本：主题名天然是一句人话。
  - 不落盘，没有 hubs/ 目录。删了重跑即可。

社区检测用贪心模块度合并（Clauset-Newman-Moore 的朴素实现）。选它而不是标签
传播的理由是**确定性**：标签传播要靠随机初始化和随机遍历顺序，同一个库两次跑
可能给出不同的主题划分，那样「我有哪些主题」这个问题就没有稳定答案了。

**每个主题都带一个内聚度判决**，这是本命令最重要的输出。理由：在稀疏图上，
一个团的边界是由「那几条桥边恰好落在哪」决定的，不是由密度决定的。把 20 条
勉强串成一串的笔记和 13 条互相咬合的笔记并排列成两个「主题」，读的人会以为
它们一样可信。所以宁可自曝：树形的团、被一条边缝起来的团，都明说。
"""

import datetime

from . import graph

# 图太稀疏时社区检测跑出来的是噪声，不是主题。契约见 derived-views.md §2。
MIN_NOTES = 40

# 边数下限：40 条互不链接的笔记同样通不过「够密」这一关。没有这条，
# 一个零边的库会得到 40 个 size=1 的「主题」，正是门槛想挡的东西。
MIN_EDGES = 20

# 小于这个规模的连通分量不是主题，是碎片。单独列，不进主题表。
MIN_TOPIC_SIZE = 4

# 删掉一条桥边后小的那半仍占全团这个比例，才判「疑似两个主题」。
# 2026-08-11 质量回填后重标（全图平均度 2.59 → 3.02）：十二个主题的实测分布
# 0.00 / 0.07 / 0.10 / 0.10 / 0.12 / 0.17 / 0.25 / 0.25 / 0.33 / 0.39 / 0.46 /
# 0.50，0.39 与 0.46 之间有天然豁口，0.40 仍切在豁口上且报出的两个经人工
# 核对都属实，维持不变。
STITCH_RATIO = 0.40

# 「疑似多个主题」的三个条件，要同时满足。单独任何一条都会误伤：
# 块数多但回路密的团（16 条那个交接主题分出 4 块，回路密度 0.31）是真的一个主题。
MULTI_MIN_BLOCKS = 4        # 子社区分解出至少这么多块 >=3 节点的
MULTI_MAX_CYCLE_RATIO = 0.30  # 且回路密度低于这个值（2026-08-11 重标，随薄线同步）

# 任何一个探测器落在阈值这个相对距离以内，就标「擦边」。
# 存在的理由：不标的话，「实」的含义只是「没有任何探测器响」，于是差 0.015
# 就该报的团和结结实实的团印出来一模一样，漏报正好藏在那里。
NEAR_MISS = 0.15

# 一块子社区要有这么多节点才算「一块」，两条笔记连一起是噪声不是子主题。
MULTI_BLOCK_MIN = 3

# 回路密度低于这个值判「薄」。2026-08-11 质量回填后重标：十二个主题落在
# 0.25 / 0.25 / 0.25 / 0.33 / 0.33 / 0.36 / 0.38 / 0.40 / 0.40 / 0.50 / 0.54 /
# 0.60，0.25 与 0.33 之间有豁口，线取 0.30 切在豁口上。
#
# 必须知道这个量在量什么：连通团里回路数 = 边数 − 点数 + 1，所以回路密度
# ≈ 团内平均度/2 − 1。**它量的是链接密度，不是主题内聚度。** 全图平均度已从
# 2.59 升到 3.02，「薄」现在的含义是「明显低于回填后的库平均」。
# 换一个链接更密或更疏的知识库会整体漂移，重标条件见 derived-views.md §2.1。
THIN_CYCLE_RATIO = 0.30

# 「最近有没有新成员」的默认窗口天数。与 frontier 一致按 event_date 算，
# 不按 git 时间：批量蒸馏会把 git 时间压平成同一天。
DEFAULT_WINDOW_DAYS = 14


class TooSparse(Exception):
    """笔记数或边数不够，社区检测无意义。"""

    def __init__(self, notes, edges):
        self.notes = notes
        self.edges = edges
        if notes < MIN_NOTES:
            reason = "只有 %d 条笔记，需要至少 %d 条" % (notes, MIN_NOTES)
        else:
            reason = "只有 %d 条边，需要至少 %d 条" % (edges, MIN_EDGES)
        self.reason = reason
        super().__init__("%s。图太稀疏时跑出来的团是噪声，不是主题。" % reason)

    def as_json(self):
        """错误也要有结构化形态：JSON 是给 agent 消费的正式 API。"""
        return {
            "error": "too_sparse",
            "reason": self.reason,
            "notes": self.notes,
            "edges": self.edges,
            "min_notes": MIN_NOTES,
            "min_edges": MIN_EDGES,
        }


def build_graph(notes):
    """Return (nodes, adjacency, edges).

    只有两端都在库里的链接才成边。悬空链接是「将来值得写」的标记，不是结构，
    让它成边会凭空造出一个不存在的节点。自环丢掉，重复边只算一次。
    """
    nodes = sorted(n.slug for n in notes)
    known = set(nodes)
    adj = {slug: set() for slug in nodes}
    edges = set()
    for n in notes:
        for target in n.links:
            if target not in known or target == n.slug:
                continue
            adj[n.slug].add(target)
            adj[target].add(n.slug)
            edges.add((min(n.slug, target), max(n.slug, target)))
    return nodes, {k: sorted(v) for k, v in adj.items()}, edges


def in_degree(notes):
    """slug -> 有多少条**不同的**笔记指向它。

    入链而不是总度数：主题的地基是「别人指向它」，它自己指向谁不构成地基。
    """
    known = {n.slug for n in notes}
    counts = {slug: 0 for slug in known}
    for n in notes:
        for target in n.links:
            if target in known and target != n.slug:
                counts[target] += 1
    return counts


def greedy_modularity(nodes, edges):
    """确定性贪心模块度合并，返回 community id -> sorted member list。

    Q = Σ_c [ l_c/m - (d_c/2m)² ]，合并 i、j 的增量是
    ΔQ = e_ij/m - d_i·d_j/(2m²)，其中 e_ij 是两个社区之间的边数。
    只在有边相连的社区之间考虑合并（无边时 ΔQ 必为负），合并到没有正增量为止。

    平局按社区 key 的字典序取第一个，所以同一个库两次跑结果完全一致。
    """
    m = len(edges)
    if m == 0:
        return {slug: [slug] for slug in nodes}

    comm = {slug: slug for slug in nodes}
    members = {slug: {slug} for slug in nodes}
    degree = {slug: 0 for slug in nodes}
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    total_deg = dict(degree)

    edge_list = sorted(edges)

    while True:
        # 一趟扫完所有边，同时得到候选社区对和它们之间的边数，
        # 省掉每对再全扫一次 edges 的平方级开销。
        pair_edges = {}
        for a, b in edge_list:
            ca, cb = comm[a], comm[b]
            if ca == cb:
                continue
            key = (ca, cb) if ca < cb else (cb, ca)
            pair_edges[key] = pair_edges.get(key, 0) + 1
        if not pair_edges:
            break

        best, best_dq = None, 0.0
        for pair in sorted(pair_edges):
            ci, cj = pair
            dq = pair_edges[pair] / m - (total_deg[ci] * total_deg[cj]) / (2.0 * m * m)
            # 严格大于：平局时保留排序靠前的那个，结果与输入顺序无关。
            if dq > best_dq:
                best, best_dq = pair, dq
        if best is None:
            break

        ci, cj = best
        keep, gone = (ci, cj) if ci < cj else (cj, ci)
        for slug in members[gone]:
            comm[slug] = keep
        members[keep] |= members[gone]
        total_deg[keep] += total_deg[gone]
        del members[gone]
        del total_deg[gone]

    return {cid: sorted(ms) for cid, ms in members.items()}


def _is_hub_flag(note):
    """`hub: true` 是例外不是制度：一条都不标也有完整的主题视图。"""
    raw = note.fm.get("hub")
    return str(raw).strip().lower() in ("true", "yes", "1")


def _event_date(note):
    try:
        return datetime.date.fromisoformat(str(note.fm.get("event_date", "")))
    except (ValueError, TypeError):
        return None


def _sub_blocks(members, sub_adj):
    """在这个团自己的子图上再跑一次社区检测，看它由哪几块拼成。"""
    sub_edges = {
        (a, b) for a in members for b in sub_adj.get(a, ()) if a < b
    }
    blocks = sorted(
        (sorted(g) for g in greedy_modularity(members, sub_edges).values()),
        key=lambda g: (-len(g), g[0]),
    )
    return blocks


def _all_bridges(members, sub_adj):
    """全部桥边，按切得多均衡排序。**不筛阈值**，最均衡的那条要一直报出来，
    哪怕它没过线。不然「实」就等于「没有任何探测器响」，而差 0.015 没响的团
    和结实的团在纸面上一模一样。"""
    size = len(members)
    out = []
    for bridge in graph.bridges(members, sub_adj):
        small, large = graph.split_sizes(members, sub_adj, bridge)
        out.append({
            "edge": list(bridge),
            "splits_into": [small, large],
            "balance": round(small / float(size), 3),
        })
    return sorted(out, key=lambda s: (-s["balance"], s["edge"]))


def _cohesion(members, sub_adj, edge_count):
    """这个团有多像一个主题，以及凭什么这么说。

    三个可核对的结构量，不是打分：
      - 回路数 = 边数 - 点数 + 1。为 0 说明它是一棵树，任何一条边都是桥，
        边界纯粹由链接恰好挂在哪决定。
      - 可割子团：割掉不超过 DETACH_MAX_CUT 条边就能整块拿走的一块，
        且它占全团四分之一以上，多半本来就是两个主题。
      - 回路密度 = 回路数 / 点数。低说明结构支撑不足。
    """
    size = len(members)
    cycles = edge_count - size + 1
    cycle_ratio = cycles / float(size)
    flags = []

    bridges = _all_bridges(members, sub_adj)
    best_balance = bridges[0]["balance"] if bridges else 0.0
    blocks = _sub_blocks(members, sub_adj)
    big_blocks = [b for b in blocks if len(b) >= MULTI_BLOCK_MIN]

    # 散成好几块、回路又撑不住，这不是一个主题被稀疏化，是几摊东西被串在
    # 一起。和「疑似两个主题」是两种病：那个有明确的一刀，这个切不出两半。
    multi = len(big_blocks) >= MULTI_MIN_BLOCKS and cycle_ratio < MULTI_MAX_CYCLE_RATIO
    stitched = best_balance >= STITCH_RATIO

    if multi:
        flags.append("多块")
    if stitched:
        flags.append("缝合")
    # 树要独立成 flag，不能被「缝合」压掉：一个每条边都是桥的团，报「有一处
    # 接缝」是在撒谎，它没有结构，报出来那条只是同样任意的 N 条里最均衡的。
    if cycles <= 0:
        flags.append("树")
    elif cycle_ratio < THIN_CYCLE_RATIO:
        flags.append("薄")

    # 擦边：任何一个探测器差一点就响。阴性也必须出示证据，否则漏报无处可见。
    near = []
    if not stitched and best_balance >= STITCH_RATIO * (1 - NEAR_MISS):
        near.append("最佳桥平衡度 %.3f，离缝合线 %.2f 只差 %.3f"
                    % (best_balance, STITCH_RATIO, STITCH_RATIO - best_balance))
    if not multi and len(big_blocks) >= MULTI_MIN_BLOCKS:
        near.append("已经散成 %d 块，只靠回路密度 %.3f 高于 %.2f 才没判多主题"
                    % (len(big_blocks), cycle_ratio, MULTI_MAX_CYCLE_RATIO))
    if cycles > 0 and THIN_CYCLE_RATIO <= cycle_ratio < THIN_CYCLE_RATIO * (1 + NEAR_MISS):
        near.append("回路密度 %.3f 刚过薄线 %.2f" % (cycle_ratio, THIN_CYCLE_RATIO))
    if near:
        flags.append("擦边")

    # 按严重度取第一个命中的。等级是单值，flags 是全部证据。
    if multi:
        grade = "疑似多个主题"
    elif stitched:
        grade = "疑似两个主题"
    elif cycles <= 0:
        grade = "边界任意"
    elif cycle_ratio < THIN_CYCLE_RATIO:
        grade = "薄"
    else:
        grade = "实"

    return {
        "grade": grade,
        "flags": flags,
        "near_misses": near,
        "cycles": cycles,
        "cycle_ratio": round(cycle_ratio, 3),
        "internal_edges": edge_count,
        "best_bridge": bridges[0] if bridges else None,
        "bridge_count": len(bridges),
        "stitches": [b for b in bridges if b["balance"] >= STITCH_RATIO],
        "blocks": big_blocks if multi else [],
        "block_count": len(big_blocks),
    }


def compute(notes, days=DEFAULT_WINDOW_DAYS, today=None):
    """算出全部主题。纯函数，不落盘，不读时钟以外的任何外部状态。"""
    nodes, adj, edges = build_graph(notes)
    if len(notes) < MIN_NOTES or len(edges) < MIN_EDGES:
        raise TooSparse(len(notes), len(edges))

    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    by_slug = {n.slug: n for n in notes}

    global_in = in_degree(notes)
    communities = greedy_modularity(nodes, edges)

    # 小于 MIN_TOPIC_SIZE 的连通分量不是社区检测的产物，是图上本来就孤立的
    # 小块。把它们当主题输出，会让一个 2 条笔记的碎片和一个 20 条的主题
    # 并排出现在同一张表里，读的人会以为它们是一个量级的东西。
    fragment_slugs = set()
    for comp in graph.components(nodes, adj):
        if len(comp) < MIN_TOPIC_SIZE:
            fragment_slugs.update(comp)

    topics, fragments = [], []
    for cid, member_slugs in communities.items():
        if set(member_slugs) <= fragment_slugs:
            fragments.append({
                "members": member_slugs,
                "size": len(member_slugs),
                "titles": [by_slug[s].title for s in member_slugs],
            })
            continue

        member_set, sub_adj, edge_count = graph.subgraph(member_slugs, adj)

        # 介数必须跑在**团内子图**上。跑全图会选出「最会跨主题串门的那条」，
        # 而那恰好是「团内地基」的反面：库里全图介数最高的那条笔记，在自己
        # 团里的介数只有 1.0，几乎是个挂件。
        local_central = graph.betweenness(member_slugs, sub_adj)

        # 社区内入链：只数来自同一主题的指向。它衡量的是「在这个主题内部，
        # 谁是被反复引用的地基」，全局入链会让跨主题的热门笔记抢走入口位。
        local_in = {slug: 0 for slug in member_slugs}
        for slug in member_slugs:
            for target in by_slug[slug].links:
                if target in member_set and target != slug:
                    local_in[target] += 1

        def rank(slug):
            # 团内入链优先（契约），平局用团内介数：在稀疏图上入链数经常三五路
            # 平票，此时按 slug 字母序选纯属任意，而团内介数能指出真正的枢纽。
            return (-local_in[slug], -local_central[slug], -global_in.get(slug, 0), slug)

        ranked = sorted(member_slugs, key=rank)
        top_in = local_in[ranked[0]]
        tied = sorted(s for s in member_slugs if local_in[s] == top_in)

        # `hub: true` 覆盖算法结果。derived-views.md §2 明确它是例外不是制度。
        flagged = [s for s in ranked if _is_hub_flag(by_slug[s])]
        entry = flagged[0] if flagged else ranked[0]

        # 入口必须出现在 top_members 里：主题名就是它的标题，它是读者第一个
        # 要核对的东西，列不出来等于主题名没有出处。
        shown = [entry] + [s for s in ranked if s != entry]
        recent = sorted(
            s for s in member_slugs
            if (_event_date(by_slug[s]) or datetime.date.min) >= cutoff
        )

        cohesion = _cohesion(member_slugs, sub_adj, edge_count)
        # 每一块给出它自己的入口和标题。一个散成五块的团，说「它有五块」没用，
        # 要能说出这五块各叫什么，那才是读者真正要的五个主题名。
        named_blocks = []
        for block in cohesion["blocks"]:
            bset = set(block)
            b_in = {s: 0 for s in block}
            for s in block:
                for tgt in by_slug[s].links:
                    if tgt in bset and tgt != s:
                        b_in[tgt] += 1
            b_entry = sorted(block, key=lambda s: (-b_in[s], -local_in[s], s))[0]
            named_blocks.append({
                "entry": b_entry,
                "name": by_slug[b_entry].title,
                "size": len(block),
                "members": block,
            })
        cohesion["blocks"] = named_blocks

        topics.append({
            "name": by_slug[entry].title,
            "entry": entry,
            # 只有当标记真的改变了结果时才算「覆盖」，否则读的人会以为
            # 不标就会变成别的。
            "entry_pinned": bool(flagged) and entry != ranked[0],
            "entry_tied": len(tied) > 1,
            "entry_tied_with": tied if len(tied) > 1 else [],
            "entry_betweenness": round(local_central[entry], 1),
            "size": len(member_slugs),
            "members": member_slugs,
            "cohesion": cohesion,
            # 散成多块且无主干时，没有任何一条笔记的标题能代表这个团。
            # 硬拿入口的标题当名字，等于用覆盖 1/20 成员的一句话给全团命名。
            "named": cohesion["grade"] != "疑似多个主题",
            "top_members": [
                {
                    "slug": s,
                    "title": by_slug[s].title,
                    "in_topic": local_in[s],
                    "in_total": global_in.get(s, 0),
                }
                for s in shown[:5]
            ],
            "recent_members": recent,
            "active": bool(recent),
        })

    topics.sort(key=lambda t: (-t["size"], t["entry"]))
    fragments.sort(key=lambda f: (-f["size"], f["members"][0]))

    # 按最终等级分桶，不按 flags：一个团可以同时是「缝合」和「树」，按 flags
    # 数会把它数两次，于是汇总各档之和对不上主题数，而等级是单值。
    by_grade = {}
    for t in topics:
        g = t["cohesion"]["grade"]
        by_grade[g] = by_grade.get(g, 0) + 1

    return {
        "notes": len(notes),
        "edges": len(edges),
        "topics": topics,
        "fragments": fragments,
        "window_days": days,
        "asof": today.isoformat(),
        "summary": {
            "topics": len(topics),
            "by_grade": by_grade,
            "fragments": len(fragments),
            "all_active": bool(topics) and all(t["active"] for t in topics),
        },
    }


def render(result):
    s = result["summary"]
    lines = [
        "%d 条笔记，%d 条边，%d 个主题，%d 个碎片（窗口 %d 天，截至 %s）"
        % (
            result["notes"],
            result["edges"],
            s["topics"],
            s["fragments"],
            result["window_days"],
            result["asof"],
        ),
        "内聚度：" + "，".join(
            "%d 个%s" % (s["by_grade"][g], g)
            for g in ("实", "薄", "边界任意", "疑似两个主题", "疑似多个主题")
            if s["by_grade"].get(g)
        ),
        "",
    ]
    for i, t in enumerate(result["topics"], start=1):
        c = t["cohesion"]
        # 等级挂在标题行上而不是只写在下一行：主题名会被单独引用、复制、
        # 塞进别的文档，而一个「薄」团的名字只覆盖它一部分成员。让警告跟着名字走。
        extra = [c["grade"]] + [f for f in c["flags"] if f in ("树", "擦边")]
        mark = "" if c["grade"] == "实" and not c["flags"] else "  [%s]" % "，".join(extra)
        if t["named"]:
            lines.append("%d. %s%s" % (i, t["name"], mark))
        else:
            lines.append("%d. （%d 块拼成，没有一条笔记能代表全团）%s"
                         % (i, len(c["blocks"]), mark))

        # 三个数每次都印，不管判成什么。「实」如果不出示余量，它的含义就只是
        # 「没有任何探测器响」，差一点没响的团和结实的团在纸面上就分不出来。
        bb = c["best_bridge"]
        lines.append(
            "   %d 条，%d 条内部边，回路密度 %.2f，最佳桥平衡度 %s，子块 %d"
            % (t["size"], c["internal_edges"], c["cycle_ratio"],
               ("%.2f" % bb["balance"]) if bb else "无桥", c["block_count"])
        )
        if "树" in c["flags"]:
            lines.append(
                "   这个团是一棵树，%d 条边全是桥，任何一条删掉都断成两截，"
                "下面那条只是其中最均衡的一条，不是「唯一的接缝」。" % c["internal_edges"]
            )
        for n in c["near_misses"]:
            lines.append("   ~ 擦边：%s" % n)
        # 判了「没有一条笔记能代表全团」就不该再印一个「入口」，那等于把刚
        # 收回去的那个名字又递出来一次。这种团的入口在下面按块给。
        if t["named"]:
            entry_note = "   入口 %s" % t["entry"]
            if t["entry_pinned"]:
                entry_note += "（hub: true 指定）"
            elif t["entry_tied"]:
                entry_note += "（入链 %d 路平票，按团内介数 %.1f 选的）" % (
                    len(t["entry_tied_with"]), t["entry_betweenness"])
            lines.append(entry_note)
        for member in t["top_members"]:
            lines.append("     %2d 入链  %s" % (member["in_topic"], member["title"]))
            lines.append("             %s" % member["slug"])
        # 只印最均衡的那一条：同一个团里报三条切法，读的人只会略过。
        for st in c["stitches"][:1]:
            lines.append(
                "   ⚠ 删掉 %s ↔ %s 这一条边就断成 %d / %d 两块，多半是两个主题"
                % (st["edge"][0], st["edge"][1], st["splits_into"][0], st["splits_into"][1])
            )
        if c["blocks"]:
            lines.append("   ⚠ 这 %d 块各自更像一个主题：" % len(c["blocks"]))
            for b in c["blocks"]:
                lines.append("       [%d 条] %s" % (b["size"], b["name"]))
                lines.append("               入口 %s" % b["entry"])
        lines.append(
            "   窗口内新增 %d/%d" % (len(t["recent_members"]), t["size"])
        )
        lines.append("")

    if result["fragments"]:
        lines.append("碎片（连通分量小于 %d，不构成主题）：" % MIN_TOPIC_SIZE)
        for f in result["fragments"]:
            lines.append("   %s" % "、".join(f["titles"]))
            lines.append("      %s" % ", ".join(f["members"]))
        lines.append("")

    if s["all_active"]:
        lines.append(
            "注：%d 个主题全部在窗口内有新成员，所以「活跃」这一维现在不区分任何东西。"
            % s["topics"]
        )
        lines.append("    整个库的 event_date 只跨两个月，等时间拉长它才有信号。")
    return "\n".join(lines).rstrip()


def render_brief(result):
    """每主题一行：内聚等级、条数、窗口内新增、入口 slug。

    给会话开场定位用：全量 render 是复核用的诊断视图，日常「库里有什么在动」
    只要这一屏。行数契约：不超过主题数 + 5（头 1 行 + 碎片 1 行 + 富余）。
    只改人读输出，--json 的形态与全量一致。
    """
    s = result["summary"]
    lines = [
        "%d 条笔记，%d 条边，%d 个主题，%d 个碎片（窗口 %d 天，截至 %s）"
        % (result["notes"], result["edges"], s["topics"], s["fragments"],
           result["window_days"], result["asof"]),
    ]
    for i, t in enumerate(result["topics"], start=1):
        c = t["cohesion"]
        # 等级必须跟着行走，理由同全量 render：主题名（这里是入口 slug）会被
        # 单独复制引用，警告不在行内等于没警告。
        marks = [c["grade"]] + [f for f in c["flags"] if f in ("树", "擦边")]
        if t["named"]:
            where = t["entry"]
        else:
            where = "（%d 块拼成，无单一入口）" % len(c["blocks"])
        lines.append("%2d. [%s] %d 条，窗口新增 %d ｜ %s"
                     % (i, "，".join(marks), t["size"], len(t["recent_members"]), where))
    if result["fragments"]:
        lines.append("碎片：%s" % ", ".join(
            m for f in result["fragments"] for m in f["members"]))
    return "\n".join(lines)
