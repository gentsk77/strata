"""Note file parsing.

零依赖：frontmatter 解析器只支持本项目 note 契约用到的 YAML 子集
(顶层 key: value，加一层两空格缩进的嵌套)。遇到不认识的语法就报错而不是猜，
因为一个静默猜错的解析器会让 doctor 的全绿变成假绿。
"""

import datetime
import re
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]|#]+)")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 这条笔记最终靠什么支撑，强度降序。判据见 spec/fields.md §2。
EVIDENCE = ("revealed", "stated", "inferred")

# 消化到哪一步。unsettled = 刚捕捉还没和库里其他东西对过。
STATUSES = ("unsettled", "settled")



class ParseError(Exception):
    pass


def _strip_quotes(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text, where):
    """Return (frontmatter_dict, body). Raises ParseError on malformed input."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ParseError("%s: 缺少 frontmatter 起始的 ---" % where)
    rest = text.split("\n", 1)[1]
    end = rest.find("\n---")
    if end == -1:
        raise ParseError("%s: frontmatter 没有闭合的 ---" % where)
    head, body = rest[:end], rest[end + 4 :].lstrip("\n")

    data, current_key = {}, None
    for lineno, raw in enumerate(head.split("\n"), start=2):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        # 列表项：`  - some/path.md`。用于 artifacts 这类多值字段。
        if indent == 2 and stripped.startswith("- "):
            if current_key is None:
                raise ParseError("%s:%d: 列表项没有父键" % (where, lineno))
            if isinstance(data.get(current_key), dict) and not data[current_key]:
                data[current_key] = []
            if not isinstance(data.get(current_key), list):
                raise ParseError("%s:%d: %s 已经是标量或映射，不能再加列表项" % (where, lineno, current_key))
            data[current_key].append(_strip_quotes(stripped[2:].strip()))
            continue

        if ":" not in line:
            raise ParseError("%s:%d: 无法解析的 frontmatter 行 %r" % (where, lineno, raw))
        key, _, value = line.strip().partition(":")
        key, value = key.strip(), _strip_quotes(value.strip())
        if indent == 0:
            current_key = key
            data[key] = value if value else {}
        elif indent == 2:
            if current_key is None or not isinstance(data.get(current_key), dict):
                raise ParseError("%s:%d: 缩进项没有父键" % (where, lineno))
            data[current_key][key] = value
        else:
            raise ParseError("%s:%d: 只支持 0 或 2 空格缩进" % (where, lineno))
    return data, body


def parse_date(value):
    """Absolute ISO date only. 'recently' / '上周' are not dates."""
    return datetime.date.fromisoformat(str(value))


def links_in(body):
    """Ordered, de-duplicated [[wikilink]] targets."""
    seen, out = set(), []
    for m in LINK_RE.finditer(body):
        slug = m.group(1).strip()
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


class Note:
    def __init__(self, path, fm, body):
        self.path = Path(path)
        self.fm = fm
        self.body = body
        self.slug = self.path.stem
        self.links = links_in(body)

    @property
    def title(self):
        return self.fm.get("title", "")

    @property
    def evidence(self):
        return self.fm.get("evidence", "")

    @property
    def status(self):
        return self.fm.get("status", "")

    @property
    def artifacts(self):
        """活工具的当前版本在哪。笔记记推理，artifact 记它现在长什么样。"""
        value = self.fm.get("artifacts")
        return value if isinstance(value, list) else []

    @classmethod
    def load(cls, path):
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text, path.name)
        return cls(path, fm, body)


def load_all(notes_dir):
    """Load every note in a flat directory.

    Returns (notes, parse_errors, stubs). A stub is an empty file: Obsidian
    creates one the moment you click a dangling link. Parsing keeps them
    separate; judging them is doctor's job (a violation since D8: an empty
    file is not a legal note, links into it are dangling in practice).
    """
    notes, errors, stubs = [], [], []
    for path in sorted(Path(notes_dir).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            stubs.append(path.stem)
            continue
        try:
            fm, body = parse_frontmatter(text, path.name)
        except ParseError as e:
            errors.append(str(e))
            continue
        notes.append(Note(path, fm, body))
    return notes, errors, stubs
