import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertTriangle, ArrowLeft, Check, MessageSquare, Play, StopCircle } from "lucide-react"

import { LinkGraph } from "@/components/link-graph"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api, subscribeToRuns } from "@/lib/api"
import {
  NOTE_STATUS,
  REVIEW_STATUS,
  RUN_KIND,
  RUN_STATUS,
  formatCost,
  formatCount,
  formatDuration,
  willRewrite,
  willSettle,
} from "@/lib/labels"
import { renderMarkdown } from "@/lib/markdown"

/* eslint-disable @typescript-eslint/no-explicit-any */

type Banner = { id: string; kind: "alarm" | "default"; title: string; text: string }

type ChipKind = "open" | "solid" | "approved" | "sent-back"

/**
 * A judgment the note has since moved out from under.
 *
 * The service keys review records on slug plus blob hash (ruling D2) and ships
 * the comparison as `reviewStale`. It has to reach the screen: an approval
 * recorded against an older text is not a judgment on what is there now, and
 * silently reading it as "Approved, waiting to settle" is the one way this app
 * can settle a note the maintainer never actually approved.
 */
function isStale(note: any): boolean {
  return note?.reviewStale === true && note?.review != null
}

function chip(note: any): { text: string; kind: ChipKind } {
  if (isStale(note)) return { text: REVIEW_STATUS.stale, kind: "open" }
  if (note?.review?.action === "approve") return { text: REVIEW_STATUS.approve, kind: "approved" }
  if (note?.review?.action === "reject") return { text: REVIEW_STATUS.reject, kind: "sent-back" }
  const status = note?.status ?? note?.frontMatter?.status
  if (status === "unsettled") return { text: NOTE_STATUS.unsettled, kind: "open" }
  return { text: NOTE_STATUS.settled, kind: "solid" }
}

function chipShort(note: any): string {
  if (isStale(note)) return "text changed since review"
  if (note?.review?.action === "approve") return "approved"
  if (note?.review?.action === "reject") return "sent back"
  const status = note?.status ?? note?.frontMatter?.status
  return status === "settled" ? "settled" : "unsettled"
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-data text-muted-foreground">{children}</span>
}

function StatusDot({ kind }: { kind: ChipKind }) {
  // open = hollow, solid = ink-filled, approved = check, sent-back = reply glyph.
  if (kind === "open") return <span className="inline-block size-1.5 rounded-full border border-foreground" aria-hidden />
  if (kind === "solid") return <span className="inline-block size-1.5 rounded-full bg-foreground" aria-hidden />
  if (kind === "approved") return <Check className="size-3 text-muted-foreground" aria-hidden />
  return <MessageSquare className="size-3 text-muted-foreground" aria-hidden />
}

export default function App() {
  const [config, setConfig] = useState<any>(null)
  const [queue, setQueue] = useState<any>(null)
  const [note, setNote] = useState<any>(null)
  const [graph, setGraph] = useState<any>(null)
  const [runs, setRuns] = useState<any[]>([])
  const [doctor, setDoctor] = useState<any>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState<"unsettled" | "all">("unsettled")
  const [scope, setScope] = useState<"batch" | "all">("batch")
  const [busy, setBusy] = useState(false)
  const [banners, setBanners] = useState<Banner[]>([])
  const [commentOpen, setCommentOpen] = useState(false)
  const [comment, setComment] = useState("")
  const [commentError, setCommentError] = useState<string | null>(null)
  const [source, setSource] = useState("")
  const [maxTurns, setMaxTurns] = useState(0)
  const [maxCost, setMaxCost] = useState(0)

  const activeRun = runs.find((run) => run.status === "running") ?? null
  const lastRun = activeRun ?? runs[0] ?? null

  const currentBatch = useMemo(() => {
    const produced = runs.find((run) => run.producedSlugs?.length > 0)?.producedSlugs
    if (produced?.length) return new Set<string>(produced)
    return new Set<string>((queue?.unsettled ?? []).map((n: any) => n.slug))
  }, [runs, queue])

  const addBanner = useCallback((id: string, kind: "alarm" | "default", title: string, text: string) => {
    setBanners((prev) => [...prev.filter((b) => b.id !== id), { id, kind, title, text }])
  }, [])
  const clearBanner = useCallback((id: string) => {
    setBanners((prev) => prev.filter((b) => b.id !== id))
  }, [])

  const refreshQueue = useCallback(async () => {
    try {
      setQueue(await api.queue())
      clearBanner("queue")
    } catch (error: any) {
      addBanner("queue", "alarm", "The queue could not be read", error.message)
    }
  }, [addBanner, clearBanner])

  const refreshRuns = useCallback(async () => {
    try {
      const payload = await api.runs()
      setRuns(payload.runs ?? [])
      setBusy((payload.runs ?? []).some((r: any) => r.status === "running"))
      clearBanner("runs")
    } catch (error: any) {
      // Without this the boot Promise.all rejects unhandled and the run panel
      // just sits on "No run yet" while every other pane loads fine.
      addBanner("runs", "alarm", "The run history could not be read", error.message)
    }
  }, [addBanner, clearBanner])

  const refreshDoctor = useCallback(async () => {
    try {
      const d = await api.doctor()
      setDoctor(d)
      if (d.ok) clearBanner("doctor")
      else addBanner("doctor", "alarm", "The library check found violations", "New runs are held until it passes.")
    } catch {
      setDoctor(null)
    }
  }, [addBanner, clearBanner])

  const refreshGraph = useCallback(async () => {
    try {
      setGraph(await api.graph())
    } catch {
      setGraph({ available: false })
    }
  }, [])

  const refreshConfig = useCallback(async () => {
    try {
      setConfig(await api.config())
    } catch (error: any) {
      addBanner("config", "alarm", "The service cannot find strata", error.message)
    }
  }, [addBanner])

  // Which slug the note pane is *supposed* to show. Two things read it:
  // `selectNote`, to drop a response that lost a race, and the SSE effect, so
  // it can refresh the open note without listing `selected` as a dependency.
  const selectedRef = useRef<string | null>(null)

  const selectNote = useCallback(
    async (slug: string) => {
      if (!slug) return
      selectedRef.current = slug
      setSelected(slug)
      setCommentOpen(false)
      setCommentError(null)
      try {
        const fetched = await api.note(slug)
        // Two fast clicks race, and the loser can resolve last. Without this
        // the rail highlights B while the pane renders A, and the next
        // Approve is recorded against B.
        if (selectedRef.current !== slug) return
        setNote(fetched)
        clearBanner("note")
      } catch (error: any) {
        if (selectedRef.current !== slug) return
        setNote(null)
        addBanner("note", "alarm", `Could not read ${slug}`, error.message)
      }
    },
    [addBanner, clearBanner],
  )

  const dispose = useCallback(
    async (action: string, commentText?: string) => {
      if (!selected) return
      try {
        const result = await api.review(selected, action, commentText)
        if (result.ok === false) {
          setCommentError(result.error)
          return
        }
        setCommentOpen(false)
        setComment("")
        setCommentError(null)
        await refreshQueue()
        await selectNote(selected)
      } catch (error: any) {
        addBanner("review", "alarm", "Could not record that", error.message)
      }
    },
    [selected, refreshQueue, selectNote, addBanner],
  )

  const startRun = useCallback(
    async (kind: "distill" | "settle" | "rewrite") => {
      if (busy) return
      setBusy(true)
      try {
        const result =
          kind === "distill" ? await api.distill(source) : kind === "settle" ? await api.settle() : await api.rewrite()
        if (result.ok === false) {
          addBanner("run", "alarm", "The run did not start", result.error)
          setBusy(false)
          return
        }
        clearBanner("run")
        await refreshRuns()
      } catch (error: any) {
        addBanner("run", "alarm", "The run did not start", error.message)
        setBusy(false)
      }
    },
    [busy, source, addBanner, clearBanner, refreshRuns],
  )

  const saveBudget = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault()
      try {
        const result = await api.setBudget(maxTurns, maxCost)
        if (result.ok === false) {
          addBanner("budget", "alarm", "Those ceilings were refused", result.error)
          return
        }
        clearBanner("budget")
        await refreshConfig()
      } catch (error: any) {
        addBanner("budget", "alarm", "Those ceilings were refused", error.message)
      }
    },
    [maxTurns, maxCost, addBanner, clearBanner, refreshConfig],
  )

  // boot
  useEffect(() => {
    // All five in parallel: none of them reads another's result. They used to
    // be chained config -> doctor -> the rest, and `/api/doctor` shells out to
    // `kb doctor`, which takes about 6.5s on the real library, so the queue
    // sat empty for that whole time on every single load.
    void Promise.all([refreshConfig(), refreshDoctor(), refreshQueue(), refreshRuns(), refreshGraph()])
  }, [refreshConfig, refreshDoctor, refreshQueue, refreshRuns, refreshGraph])

  // defaults once config lands
  useEffect(() => {
    if (config?.watchlist?.length && !source) {
      setSource(config.watchlist.find((w: any) => w.exists)?.name ?? config.watchlist[0].name)
    }
  }, [config, source])

  // Seed the budget fields once, not on every refreshConfig. `refreshConfig`
  // fires on every SSE `finished`, and re-seeding there overwrote whatever the
  // maintainer had typed but not yet saved.
  const budgetSeeded = useRef(false)
  useEffect(() => {
    if (budgetSeeded.current || !config?.budget) return
    budgetSeeded.current = true
    setMaxTurns(config.budget.maxTurns)
    setMaxCost(config.budget.maxCostUsd)
  }, [config])

  // first note once queue lands
  useEffect(() => {
    if (selected === null && queue?.unsettled?.[0]) void selectNote(queue.unsettled[0].slug)
  }, [queue, selected, selectNote])

  // SSE
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("static") === "1") return
    const unsubscribe = subscribeToRuns((event: any) => {
      if (event.kind === "rate_limit") {
        const info = event.payload ?? {}
        if (info.status !== "allowed") {
          addBanner("rate", "alarm", "Rate limited", `The ${info.rateLimitType} window reports ${info.status}. Runs are paused.`)
        }
        return
      }
      if (event.kind === "finished") {
        void (async () => {
          await Promise.all([refreshQueue(), refreshRuns(), refreshDoctor(), refreshGraph()])
          await refreshConfig()
          const open = selectedRef.current
          if (open) await selectNote(open)
        })()
        return
      }
      void refreshRuns()
    })
    return unsubscribe
    // `selected` is deliberately absent: it changes on every queue click, and
    // listing it here tore the stream down and reopened it each time. The
    // service pushes only to live subscribers (no buffer, no Last-Event-ID),
    // so a `finished` event landing in that reconnect window was lost, `busy`
    // stuck true and every run button stayed disabled until a page reload.
    // The open slug is read through `selectedRef` instead.
  }, [addBanner, refreshQueue, refreshRuns, refreshDoctor, refreshGraph, refreshConfig, selectNote])

  const shownNotes = filter === "all" ? (queue?.notes ?? []) : (queue?.unsettled ?? [])
  const knownSlugs = useMemo(() => new Set<string>((queue?.notes ?? []).map((n: any) => n.slug)), [queue])
  const noteChip = note ? chip(note) : null
  const settleCount = queue?.counts?.approvedPending ?? 0
  const rewriteCount = queue?.counts?.rejectedPending ?? 0

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
      {/* masthead */}
      <header className="flex shrink-0 items-center gap-4 border-b border-border px-6 py-3">
        <span className="font-serif text-body">strata</span>
        {doctor != null && (
          <Eyebrow>{doctor.ok ? `${doctor.notes} notes, clean` : "doctor found violations"}</Eyebrow>
        )}

        <span className="ml-auto flex items-center gap-3">
          <Select value={source} onValueChange={setSource}>
            <SelectTrigger className="h-8 w-44" aria-label="Watchlist source to distill">
              <SelectValue placeholder="Watchlist source" />
            </SelectTrigger>
            <SelectContent>
              {(config?.watchlist ?? []).map((entry: any) => (
                <SelectItem key={entry.name} value={entry.name} disabled={!entry.exists}>
                  {entry.exists ? entry.name : `${entry.name} (missing)`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button size="sm" onClick={() => startRun("distill")} disabled={busy}>
            <Play /> Distill
          </Button>
          <Button size="sm" variant="outline" onClick={() => startRun("settle")} disabled={busy || settleCount === 0} title={settleCount > 0 ? willSettle(settleCount) : "Nothing approved yet"}>
            <Check /> Settle approved
          </Button>
          <Button size="sm" variant="outline" onClick={() => startRun("rewrite")} disabled={busy || rewriteCount === 0} title={rewriteCount > 0 ? willRewrite(rewriteCount) : "Nothing declined yet"}>
            <ArrowLeft /> Rewrite declined
          </Button>
          {activeRun != null && (
            <Button size="sm" variant="ghost" onClick={() => api.stop().then(refreshRuns)}>
              <StopCircle /> Stop run
            </Button>
          )}

          {activeRun != null && (
            <span className="flex items-center gap-1.5 font-mono text-data">
              <span className="inline-block size-1.5 animate-live-dot rounded-full bg-foreground" />
              {RUN_KIND[activeRun.kind] ?? activeRun.kind} running
            </span>
          )}

          <span className="ml-2 font-mono text-xs tabular-nums text-muted-foreground">
            today {formatCost(config?.spend?.today)}, estimated
          </span>
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* queue rail */}
        <aside className="flex w-52 shrink-0 flex-col border-r border-border">
          <div className="flex items-baseline justify-between px-5 pb-2 pt-4">
            <Eyebrow>queue</Eyebrow>
            <span className="font-mono text-data tabular-nums text-muted-foreground">{shownNotes.length}</span>
          </div>
          <nav className="min-h-0 flex-1 overflow-y-auto px-5">
            {queue == null
              ? Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-10 border-b border-border/60 bg-muted/40" aria-busy />)
              : shownNotes.length === 0
                ? <p className="pt-4 text-sm text-muted-foreground">{filter === "unsettled" ? "Nothing is waiting." : "No notes yet."}</p>
                : shownNotes.map((n: any) => {
                    const isSelected = n.slug === selected
                    return (
                      <button
                        key={n.slug}
                        type="button"
                        onClick={() => selectNote(n.slug)}
                        className={`block w-full border-b border-border/60 py-3 text-left transition-colors hover:bg-muted/40 ${isSelected ? "bg-muted/50" : ""}`}
                      >
                        <span className="flex items-start gap-2">
                          <span className="mt-1.5 shrink-0"><StatusDot kind={chip(n).kind} /></span>
                          <span className="min-w-0">
                            <span className="block font-serif text-body leading-snug">{n.title || n.slug}</span>
                            <span className="mt-1 block font-mono text-data text-muted-foreground">
                              {n.slug} · {chipShort(n)}
                              {n.commentCount > 0 ? ` · ${n.commentCount} comment${n.commentCount === 1 ? "" : "s"}` : ""}
                            </span>
                          </span>
                        </span>
                      </button>
                    )
                  })}
          </nav>
          <div className="flex items-center gap-4 border-t border-border px-5 py-3 font-mono text-data">
            <button className={filter === "unsettled" ? "text-foreground underline decoration-1 underline-offset-4" : "text-muted-foreground hover:text-foreground"} onClick={() => setFilter("unsettled")}>
              Unsettled
            </button>
            <button className={filter === "all" ? "text-foreground underline decoration-1 underline-offset-4" : "text-muted-foreground hover:text-foreground"} onClick={() => setFilter("all")}>
              All
            </button>
          </div>
        </aside>

        {/* graph pane */}
        <section className="flex min-h-0 min-w-0 flex-1 flex-col border-r border-border">
          <div className="flex items-baseline gap-3 px-5 pb-2 pt-4">
            <Eyebrow>graph</Eyebrow>
            <span className="font-sans text-sm">
              {scope === "all" ? `Whole library, ${graph?.counts?.nodes ?? 0} notes` : "This batch and its neighbours"}
            </span>
            <span className="ml-auto flex items-center gap-4 font-mono text-data">
              <button className={scope === "batch" ? "text-foreground underline decoration-1 underline-offset-4" : "text-muted-foreground hover:text-foreground"} onClick={() => setScope("batch")}>
                This batch
              </button>
              <button className={scope === "all" ? "text-foreground underline decoration-1 underline-offset-4" : "text-muted-foreground hover:text-foreground"} onClick={() => setScope("all")}>
                Whole library
              </button>
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-4">
            <div className="h-80">
              <LinkGraph graph={graph} scope={scope} focus={currentBatch} selected={selected} onSelect={(slug) => selectNote(slug)} />
            </div>

            {/* run + budget */}
            <div className="mt-6 border-t border-border pt-4">
              {lastRun == null ? (
                <p className="text-sm text-muted-foreground">No run yet. Pick a source and distill it.</p>
              ) : (
                <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5">
                  {[
                    ["run", `${RUN_KIND[lastRun.kind] ?? lastRun.kind} · ${RUN_STATUS[lastRun.status] ?? lastRun.status}`],
                    ["turns", formatCount(lastRun.numTurns)],
                    ["input", formatCount(lastRun.usage?.inputTokens)],
                    ["cache read", formatCount(lastRun.usage?.cacheReadTokens)],
                    ["output", formatCount(lastRun.usage?.outputTokens)],
                    ["elapsed", formatDuration(lastRun.durationMs)],
                    ["cost", `${formatCost(lastRun.estimatedCostUsd)} of ${formatCost(config?.budget?.maxCostUsd)}, estimated`],
                    ["notes produced", formatCount(lastRun.producedSlugs?.length ?? 0)],
                  ].map(([label, value]) => (
                    <div key={label} className="flex items-baseline justify-between gap-4 border-b border-border/50 pb-1.5">
                      <dt className="font-mono text-data text-muted-foreground">{label}</dt>
                      <dd className="font-mono text-xs tabular-nums">{value}</dd>
                    </div>
                  ))}
                </dl>
              )}

              <form onSubmit={saveBudget} className="mt-4 flex items-end gap-3">
                <div className="flex flex-col gap-1">
                  <Label className="font-mono text-data text-muted-foreground">Turn ceiling</Label>
                  <Input type="number" min={1} step={1} value={maxTurns} onChange={(event) => setMaxTurns(Number(event.target.value))} className="h-8 w-28 font-sans tabular-nums" />
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="font-mono text-data text-muted-foreground">Cost ceiling, estimated USD</Label>
                  <Input type="number" min={0.01} step={0.01} value={maxCost} onChange={(event) => setMaxCost(Number(event.target.value))} className="h-8 w-32 font-sans tabular-nums" />
                </div>
                <Button type="submit" size="sm" variant="outline">Save ceilings</Button>
              </form>
            </div>
          </div>
        </section>

        {/* note pane */}
        <section className="flex min-h-0 min-w-0 flex-[1.15] flex-col">
          <div className="px-8 pt-6">
            <Eyebrow>{note ? `${note.slug} · ${noteChip?.text ?? ""}` : "No note selected"}</Eyebrow>
            <h1 className="mt-3 font-serif text-note-title">{note?.frontMatter?.title ?? note?.slug ?? "No note selected"}</h1>
          </div>

          <article className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
            {banners.length > 0 && (
              <div className="mx-auto mb-6 max-w-[68ch] space-y-2">
                {banners.map((b) => (
                  <div key={b.id} className={`flex gap-2 border px-3 py-2 text-sm ${b.kind === "alarm" ? "border-destructive/30 bg-destructive/5 text-destructive" : "border-border bg-muted/40"}`}>
                    <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                    <div>
                      <div className="font-medium">{b.title}</div>
                      <div className="text-xs text-muted-foreground">{b.text}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="mx-auto max-w-[68ch]">
              {note == null ? (
                <p className="text-sm text-muted-foreground">Pick a note from the queue to read it.</p>
              ) : (
                <>
                  {isStale(note) && (
                    <div className="mb-6 flex gap-2 border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                      <div>
                        <div className="font-medium">This note changed after it was reviewed.</div>
                        <div className="text-xs text-muted-foreground">
                          The earlier judgment covered a different version
                          {note.review?.blob ? ` (${String(note.review.blob).slice(0, 10)})` : ""}. Read it again and
                          dispose of it afresh.
                        </div>
                      </div>
                    </div>
                  )}
                  <div className="mb-6 flex flex-wrap gap-x-4 gap-y-1 font-mono text-data text-muted-foreground">
                    {note.frontMatter?.evidence && <span>evidence {note.frontMatter.evidence}</span>}
                    {note.frontMatter?.event_date && <span>event {note.frontMatter.event_date}</span>}
                    {note.blob && <span>blob {String(note.blob).slice(0, 10)}</span>}
                    {note.frontMatter?.source && <span>source {note.frontMatter.source}</span>}
                  </div>
                  <div className="note">{renderMarkdown(note.body, { knownSlugs, onSelectNote: selectNote })}</div>

                  {(note.thread ?? []).length > 0 && (
                    <div className="mt-8 border-t border-border pt-4">
                      {note.thread.map((c: any, i: number) => (
                        <div key={i} className="mb-4">
                          <div className="flex items-baseline gap-2 font-mono text-data text-muted-foreground">
                            <span className="text-foreground">{c.author === "maintainer" ? "Maintainer" : "Rewrite run"}</span>
                            <span>{new Date(c.at).toLocaleString("en-GB", { hour12: false })}</span>
                          </div>
                          <p className="mt-1 text-sm">{c.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </article>

          {/* disposition */}
          <div className="border-t border-border px-6 py-4">
            {note != null && (
              <>
                {commentOpen ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault()
                      void dispose("reject", comment)
                    }}
                  >
                    <Label className="mb-1 block font-mono text-data text-muted-foreground">What needs to change</Label>
                    <Textarea
                      rows={3}
                      value={comment}
                      onChange={(event) => setComment(event.target.value)}
                      autoFocus
                      placeholder="The rewrite run reads every comment on this note, not just the last one."
                      className="font-sans"
                    />
                    {commentError != null && <p className="mt-1 text-xs text-destructive">{commentError}</p>}
                    <div className="mt-2 flex items-center gap-2">
                      <Button type="submit" size="sm">Send it back</Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => { setCommentOpen(false); setCommentError(null) }}>Cancel</Button>
                    </div>
                  </form>
                ) : (
                  <div className="flex items-center gap-3">
                    <Button size="lg" onClick={() => dispose("approve")}>
                      <Check /> Approve
                    </Button>
                    <Button size="lg" variant="outline" onClick={() => setCommentOpen(true)}>
                      <MessageSquare /> Send back
                    </Button>
                    <span className="ml-auto">
                      <Button size="lg" variant="ghost" onClick={() => dispose("skip")}>Skip</Button>
                    </span>
                  </div>
                )}
                <p className="mt-3 font-sans text-xs text-muted-foreground">
                  {note.blob
                    ? `Approving records your judgment on version ${String(note.blob).slice(0, 10)}. It settles only if a settle run agrees.`
                    : ""}
                </p>
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
