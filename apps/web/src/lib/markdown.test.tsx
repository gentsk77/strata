import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { joinWrapped, renderMarkdown } from "./markdown"

function html(source: string, opts: Parameters<typeof renderMarkdown>[1] = {}) {
  return renderToStaticMarkup(<>{renderMarkdown(source, opts)}</>)
}

describe("renderMarkdown", () => {
  it("escapes markup; a note cannot inject html", () => {
    const out = html('a <img src=x onerror=alert(1)> b')
    expect(out).not.toContain("<img")
    expect(out).toContain("&lt;img")
  })

  it("renders a wikilink as an anchor to the note", () => {
    const out = html("see [[other-note]] for more")
    expect(out).toContain('href="#other-note"')
    expect(out).toContain("other-note</a>")
  })

  it("marks a wikilink whose target is unknown", () => {
    const out = html("see [[ghost]]", { knownSlugs: new Set(["real"]) })
    expect(out).toContain("opacity-60")
  })

  it("uses the label form of a wikilink", () => {
    expect(html("[[a-slug|人话标题]]")).toContain(">人话标题</a>")
  })

  it("leaves emphasis inside a code span alone", () => {
    const out = html("use `a ** b` here")
    expect(out).toContain("<code>a ** b</code>")
    expect(out).not.toContain("<strong>")
  })

  it("renders bold and italic", () => {
    const out = html("**loud** and *quiet*")
    expect(out).toContain("<strong>loud</strong>")
    expect(out).toContain("<em>quiet</em>")
  })

  it("renders a fenced code block without formatting contents", () => {
    const out = html("```bash\npython -m kb doctor\n```")
    expect(out).toContain('<pre><code class="language-bash">python -m kb doctor</code></pre>')
  })

  it("renders headings one level down", () => {
    expect(html("## 小标题")).toContain("<h3>小标题</h3>")
  })

  it("renders lists", () => {
    expect(html("- one\n- two")).toContain("<ul><li>one</li><li>two</li></ul>")
    expect(html("1. one\n2. two")).toContain("<ol><li>one</li><li>two</li></ol>")
  })

  it("renders a pipe table with a head row", () => {
    const out = html("| a | b |\n|---|---|\n| 1 | 2 |")
    expect(out).toContain("<thead><tr><th>a</th><th>b</th></tr></thead>")
    expect(out).toContain("<td>1</td><td>2</td>")
  })

  it("renders a blockquote", () => {
    expect(html("> quoted")).toContain("<blockquote>quoted</blockquote>")
  })

  it("opens external links in a new tab with no referrer", () => {
    const out = html("[docs](https://example.com/x)")
    expect(out).toContain('href="https://example.com/x"')
    expect(out).toContain('rel="noreferrer noopener"')
    expect(out).toContain('target="_blank"')
  })

  it("does not turn a javascript: url into a link", () => {
    expect(html("[click](javascript:alert(1))")).not.toContain("<a")
  })

  it("survives an empty body", () => {
    expect(html("")).toBe("")
  })
})

describe("joinWrapped", () => {
  it("joins hard-wrapped Chinese with no space, because a space there is visible", () => {
    expect(joinWrapped(["成本驱动是轮数", "乘以重放的上下文"])).toBe("成本驱动是轮数乘以重放的上下文")
  })
  it("keeps the space when either side is not CJK", () => {
    expect(joinWrapped(["the agent", "replays context"])).toBe("the agent replays context")
    expect(joinWrapped(["中文接", "english"])).toBe("中文接 english")
  })
  it("leaves a single line untouched", () => {
    expect(joinWrapped(["only"])).toBe("only")
  })
})
