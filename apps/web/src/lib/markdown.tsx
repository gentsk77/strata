import * as React from "react"
import type { ReactNode } from "react"

// A small React markdown renderer for strata's notes, ported from v0's
// markdown.js. React auto-escapes text nodes, so the manual escaping is gone;
// the note format's surface, soft-wrap joining and CJK-aware joins remain.

export interface MarkdownOptions {
  knownSlugs?: Set<string>
  onSelectNote?: (slug: string) => void
}

const CJK = /[　-鿿＀-￯]/

export function joinWrapped(lines: string[]): string {
  return lines.reduce((acc, line, i) => {
    if (i === 0) return line
    const left = acc.slice(-1)
    const right = line.slice(0, 1)
    return acc + (CJK.test(left) && CJK.test(right) ? "" : " ") + line
  }, "")
}

// Inline scanner (no regex): code spans, [[wikilink]], [text](url), **bold**,
// *italic*, then plain runs. Recursion lets emphasis wrap a wikilink the way
// the original's ordered replaces did.
function renderInline(text: string, opts: MarkdownOptions): ReactNode[] {
  const out: ReactNode[] = []
  let key = 0
  let i = 0
  const n = text.length

  const pushLink = (href: string, label: string, external: boolean) => {
    out.push(
      external ? (
        <a key={key++} href={href} rel="noreferrer noopener" target="_blank" className="underline decoration-1 underline-offset-2">
          {label}
        </a>
      ) : (
        <a
          key={key++}
          href={"#" + href}
          className={"underline decoration-1 underline-offset-2 " + (opts.knownSlugs === undefined || opts.knownSlugs.has(href) ? "" : "opacity-60")}
          onClick={(event) => {
            event.preventDefault()
            opts.onSelectNote?.(href)
          }}
        >
          {label}
        </a>
      ),
    )
  }

  while (i < n) {
    const ch = text[i]
    if (ch === "`") {
      const end = text.indexOf("`", i + 1)
      if (end === -1) {
        out.push(text.slice(i))
        break
      }
      out.push(<code key={key++}>{text.slice(i + 1, end)}</code>)
      i = end + 1
    } else if (ch === "[" && text.startsWith("[[", i)) {
      const close = text.indexOf("]]", i + 2)
      if (close === -1) {
        out.push(text.slice(i))
        break
      }
      const inner = text.slice(i + 2, close)
      const bar = inner.indexOf("|")
      const target = (bar === -1 ? inner : inner.slice(0, bar)).trim()
      const label = (bar === -1 ? inner : inner.slice(bar + 1)).trim()
      pushLink(target, label, false)
      i = close + 2
    } else if (ch === "[") {
      const closeBracket = text.indexOf("]", i + 1)
      if (closeBracket !== -1 && text[closeBracket + 1] === "(") {
        const closeParen = text.indexOf(")", closeBracket + 2)
        if (closeParen !== -1) {
          const label = text.slice(i + 1, closeBracket)
          const href = text.slice(closeBracket + 2, closeParen)
          if (href.startsWith("http://") || href.startsWith("https://")) {
            pushLink(href, label, true)
            i = closeParen + 1
            continue
          }
        }
      }
      out.push(ch)
      i += 1
    } else if (ch === "*") {
      if (text.startsWith("**", i)) {
        const end = text.indexOf("**", i + 2)
        if (end !== -1) {
          out.push(<strong key={key++}>{renderInline(text.slice(i + 2, end), opts)}</strong>)
          i = end + 2
          continue
        }
      } else {
        const end = text.indexOf("*", i + 1)
        if (end !== -1 && end > i + 1) {
          out.push(<em key={key++}>{renderInline(text.slice(i + 1, end), opts)}</em>)
          i = end + 1
          continue
        }
      }
      out.push(ch)
      i += 1
    } else {
      let j = i
      while (j < n && text[j] !== "`" && text[j] !== "[" && text[j] !== "*") j += 1
      out.push(text.slice(i, j))
      i = j
    }
  }
  return out
}

function isTableRow(line: string): boolean {
  return line.includes("|") && line.trim().startsWith("|")
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim())
}

export function renderMarkdown(source: string, opts: MarkdownOptions = {}): ReactNode {
  const lines = String(source ?? "").replace(/\r\n/g, "\n").split("\n")
  const blocks: ReactNode[] = []
  let index = 0
  let key = 0

  while (index < lines.length) {
    const line = lines[index]!

    if (line.trim() === "") {
      index += 1
      continue
    }

    const fence = line.match(/^```(\w*)\s*$/)
    if (fence) {
      const body: string[] = []
      index += 1
      while (index < lines.length && !/^```\s*$/.test(lines[index]!)) {
        body.push(lines[index]!)
        index += 1
      }
      index += 1
      const language = fence[1] ? "language-" + fence[1] : ""
      blocks.push(
        <pre key={key++}>
          <code className={language || undefined}>{body.join("\n")}</code>
        </pre>,
      )
      continue
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/)
    if (heading) {
      // Note bodies start at h2: the note title is the h1, rendered separately.
      const level = Math.min(6, heading[1]!.length + 1)
      blocks.push(React.createElement("h" + level, { key: key++ }, renderInline(heading[2]!, opts)))
      index += 1
      continue
    }

    if (/^(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={key++} />)
      index += 1
      continue
    }

    if (isTableRow(line)) {
      const rows: string[] = []
      while (index < lines.length && isTableRow(lines[index]!)) {
        rows.push(lines[index]!)
        index += 1
      }
      const cells = rows.map(splitRow)
      const hasDivider = cells.length > 1 && cells[1]!.every((cell) => /^:?-{2,}:?$/.test(cell))
      const head = hasDivider ? cells[0] : null
      const body = hasDivider ? cells.slice(2) : cells
      blocks.push(
        <table key={key++}>
          {head ? (
            <thead>
              <tr>{head.map((cell, i) => <th key={i}>{renderInline(cell, opts)}</th>)}</tr>
            </thead>
          ) : null}
          <tbody>
            {body.map((row, i) => (
              <tr key={i}>{row.map((cell, j) => <td key={j}>{renderInline(cell, opts)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      )
      continue
    }

    if (/^>\s?/.test(line)) {
      const body: string[] = []
      while (index < lines.length && /^>\s?/.test(lines[index]!)) {
        body.push(lines[index]!.replace(/^>\s?/, ""))
        index += 1
      }
      blocks.push(<blockquote key={key++}>{renderInline(joinWrapped(body), opts)}</blockquote>)
      continue
    }

    const bullet = /^\s*[-*+]\s+/
    const numbered = /^\s*\d+[.)]\s+/
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line) && !bullet.test(line)
      const pattern = ordered ? numbered : bullet
      const items: ReactNode[] = []
      while (index < lines.length && pattern.test(lines[index]!)) {
        let item = lines[index]!.replace(pattern, "")
        index += 1
        while (index < lines.length && lines[index]!.trim() !== "" && !bullet.test(lines[index]!) && !numbered.test(lines[index]!)) {
          item += " " + lines[index]!.trim()
          index += 1
        }
        items.push(<li key={items.length}>{renderInline(item, opts)}</li>)
      }
      blocks.push(ordered ? <ol key={key++}>{items}</ol> : <ul key={key++}>{items}</ul>)
      continue
    }

    // Paragraph: consecutive non-blank lines, soft-wrapped.
    const paragraph: string[] = []
    while (
      index < lines.length &&
      lines[index]!.trim() !== "" &&
      !/^```/.test(lines[index]!) &&
      !/^#{1,6}\s/.test(lines[index]!) &&
      !/^>\s?/.test(lines[index]!) &&
      !isTableRow(lines[index]!) &&
      !bullet.test(lines[index]!) &&
      !numbered.test(lines[index]!)
    ) {
      paragraph.push(lines[index]!.trim())
      index += 1
    }
    blocks.push(<p key={key++}>{renderInline(joinWrapped(paragraph), opts)}</p>)
  }

  return <>{blocks}</>
}
