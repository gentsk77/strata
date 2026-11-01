"""kb doctor: check every note invariant.

不变量编号与 DESIGN 的开工文档 §3.4 一一对应：

  N1  slug 唯一、kebab-case、等于文件名，且永不改名（链接稳定性的根基）
  N2  frontmatter 六字段齐全，evidence 与 status 在枚举内，event_date 是绝对
      日期且不晚于入库时间
  N3  孤儿只报告不强制（一条「必须有链接」的硬规则交给 agent 代笔，就是在
      诱发伪造连接）
  N4  悬空链接与空文件占位一律违规（2026-08-11 维护者裁决，decisions.md D8）。
      违规信息附近似名提示，由当事会话 follow up 修掉
  N5  source 必填且是一行能定位回原文的字符串
  N6  artifact 指针指向的活文件必须还在（失效只报告，不算违规）

违规 (violation) 会让 doctor 返回非零；报告 (orphan / stale artifact) 不会。
"""

import datetime
import subprocess
from pathlib import Path

from .notes import EVIDENCE, SLUG_RE, STATUSES, load_all, parse_date

REQUIRED_FIELDS = ("id", "title", "evidence", "status", "event_date", "source")


def _git_commit_date(path):
    """Date the file last entered git, or None if never committed."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(path)],
            cwd=str(Path(path).parent),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = out.stdout.strip()
    if not stamp:
        return None
    try:
        return datetime.date.fromisoformat(stamp)
    except ValueError:
        return None


# slug 里几乎不携带语义的连接词，参与相似度计算只会制造假阳性。
_STOP = {
    "is", "are", "the", "a", "an", "of", "to", "in", "on", "for", "and", "or",
    "not", "no", "it", "that", "what", "why", "how", "be", "as", "at", "by",
}


def _tokens(slug):
    return {t for t in slug.split("-") if t and t not in _STOP}


def _near_misses(target, known, threshold=0.34):
    """已有 slug 里与 target 词元高度重叠的，多半是本该链过去却编了新名字。"""
    tt = _tokens(target)
    if not tt:
        return []
    scored = []
    for slug in known:
        ts = _tokens(slug)
        if not ts:
            continue
        overlap = len(tt & ts) / len(tt | ts)
        if overlap >= threshold:
            scored.append((overlap, slug))
    return [slug for _, slug in sorted(scored, reverse=True)[:3]]


def _workspace_root(notes_dir):
    """artifact 路径写成 <repo>/<path>，相对 strata 所在的 workspace 目录解析。

    不能直接取 notes/ 的上两级：在 git worktree 里那是 `.claude/worktrees/`，
    全部 artifact 指针会整批误报（2026-08-12 实测 52 条）。所以先问 git 主
    repo 的 .git 在哪（`--git-common-dir` 在 worktree 里指回主 repo 的 .git），
    据此上溯出 workspace。git 不可用或目录不在任何 repo 里时退回上两级，
    与修复前行为一致。
    """
    root = Path(notes_dir).resolve().parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return root.parent
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return root.parent
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = root / common_path
    return common_path.resolve().parent.parent


def check(notes_dir):
    notes, parse_errors, stubs = load_all(notes_dir)
    workspace = _workspace_root(notes_dir)
    stale_artifacts = []
    violations = [{"rule": "N0", "slug": None, "msg": e} for e in parse_errors]
    dangling, orphans = [], []

    def bad(rule, slug, msg):
        violations.append({"rule": rule, "slug": slug, "msg": msg})

    # 空文件占住的 slug 仍算 known，免得同一处问题被记两笔：占位本身单独违规，
    # 指向它的链接不再重复报悬空。
    known = {n.slug for n in notes} | set(stubs)
    seen_ids = {}

    # N4 空文件占位：空文件不是合法笔记，指向它的链接实际悬空（D8 关掉的后门）。
    for s in sorted(stubs):
        bad("N4", s, "空文件占位。空文件不是合法笔记，指向它的链接实际悬空")

    for n in notes:
        # N1 slug 唯一、kebab-case、与文件名一致
        if not SLUG_RE.match(n.slug):
            bad("N1", n.slug, "文件名不是 kebab-case slug")
        note_id = n.fm.get("id")
        if note_id and note_id != n.slug:
            bad("N1", n.slug, "frontmatter id %r 与文件名不一致" % note_id)
        if note_id in seen_ids:
            bad("N1", n.slug, "id 与 %s 重复" % seen_ids[note_id])
        elif note_id:
            seen_ids[note_id] = n.slug

        # N2 五字段齐全 + 日期可信
        for field in REQUIRED_FIELDS:
            if not n.fm.get(field):
                bad("N2", n.slug, "缺 frontmatter 字段 %s" % field)
        if n.fm.get("event_date"):
            try:
                event = parse_date(n.fm["event_date"])
            except ValueError:
                bad("N2", n.slug, "event_date %r 不是绝对日期 YYYY-MM-DD" % n.fm["event_date"])
            else:
                committed = _git_commit_date(n.path)
                limit = committed or datetime.date.today()
                if event > limit:
                    bad("N2", n.slug, "event_date %s 晚于入库时间 %s" % (event, limit))
        if n.evidence and n.evidence not in EVIDENCE:
            bad("N2", n.slug, "evidence %r 不在 %s 内" % (n.evidence, list(EVIDENCE)))
        if n.status and n.status not in STATUSES:
            bad("N2", n.slug, "status %r 不在 %s 内" % (n.status, list(STATUSES)))

        # N3 孤儿只报告，不强制。
        # 原规则要求 permanent 笔记必须有链接，2026-08-10 撤销：一条「必须有链接」的
        # 硬规则交给 agent 代笔，就是在系统性地诱发伪造连接（当天真的发生了三次）。
        # 孤儿数量本身仍是有用信号（= 还没消化），所以保留报告。
        if n.status == "settled" and not n.links:
            orphans.append(n.slug)

        # N4 悬空链接：违规（2026-08-11 维护者裁决，D8）。近似名提示照给，
        # 多数悬空是「该连的已有笔记没连上，却编了个近似的新名字」，
        # 靠 slug 词元重叠度指回正主，让 follow up 一步到位。
        for target in n.links:
            if target not in known:
                entry = {"from": n.slug, "to": target}
                near = _near_misses(target, known)
                if near:
                    entry["maybe_meant"] = near
                dangling.append(entry)
                hint = "（是不是想链已有的 %s？）" % "、".join(near) if near else ""
                bad("N4", n.slug, "悬空链接 [[%s]]，库里没有这条笔记%s" % (target, hint))

        # N6 artifact 指针：笔记记推理，artifact 指向体现这条推理的活文件。
        # 文件被移走或改名不是违规（推理仍然成立），但必须知道，否则笔记会悄悄空指。
        for rel in n.artifacts:
            if not (workspace / rel).exists():
                stale_artifacts.append({"slug": n.slug, "path": rel})

        # N5 来源可追溯：一行字符串，能定位回原文
        source = n.fm.get("source")
        if source and not isinstance(source, str):
            bad("N5", n.slug, "source 必须是一行字符串，不是嵌套块")

    return {
        "notes": len(notes),
        "violations": violations,
        "dangling": dangling,
        "orphans": orphans,
        "stubs": sorted(stubs),
        "stale_artifacts": stale_artifacts,
    }


def render(result):
    lines = ["检查了 %d 条笔记" % result["notes"]]
    if result["violations"]:
        lines.append("")
        lines.append("违规 %d 条：" % len(result["violations"]))
        for v in result["violations"]:
            lines.append("  [%s] %s: %s" % (v["rule"], v["slug"] or "-", v["msg"]))
    else:
        lines.append("违规 0 条，全绿")
    if result.get("stale_artifacts"):
        lines.append("")
        lines.append("artifact 指针失效 %d 条（文件被移走或改名，推理还在但指不到了）：" % len(result["stale_artifacts"]))
        for a in result["stale_artifacts"]:
            lines.append("  %s -> %s" % (a["slug"], a["path"]))
    if result["orphans"]:
        lines.append("")
        lines.append(
            "孤儿 %d 条（还没挂进图里 = 还没消化，不是错误）：%s"
            % (len(result["orphans"]), ", ".join(result["orphans"]))
        )
    return "\n".join(lines)
