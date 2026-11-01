"""正文层面的文本工具：句子切分与定位。

`query` 的命中片段和 `related` 的边理由句都要回答同一个问题：「正文里包含 X 的
那个句子是哪句」。这个定义只能有一份，两个命令共用这里的实现，各写一份的话，
同一条链接在两个命令里会给出两句不同的「理由」。
"""

import re

# 句末标点：中英文句号、问叹号、分号。换行永远断句，列表项一行一个意思。
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")

# markdown 行首记号（列表、引用、标题、有序列表）不算句子内容。
_LINE_PREFIX = re.compile(r"^(?:[-*>]\s+|#+\s+|\d+\.\s+)")


def sentences(text):
    """把正文切成句子列表，保序。行首 markdown 记号剥掉，空行丢掉。"""
    out = []
    for line in text.split("\n"):
        line = _LINE_PREFIX.sub("", line.strip())
        if not line:
            continue
        for part in _SENT_SPLIT.split(line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def sentence_containing(text, needle, fold=True):
    """正文里第一个包含 needle 的句子；没有则 None。fold=True 时大小写不敏感。"""
    probe = needle.casefold() if fold else needle
    for s in sentences(text):
        hay = s.casefold() if fold else s
        if probe in hay:
            return s
    return None
