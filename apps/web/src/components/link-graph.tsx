import { useLayoutEffect, useMemo, useRef, useState } from "react"
import { layout, selectSubgraph, truncate } from "@/lib/graph"
import type { GraphEdge, GraphNode } from "@/lib/graph"

/* eslint-disable @typescript-eslint/no-explicit-any */

export function LinkGraph({
  graph,
  scope,
  focus,
  selected,
  onSelect,
}: {
  graph: any
  scope: string
  focus: Set<string>
  selected: string | null
  onSelect: (slug: string) => void
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 600, height: 320 })

  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    const measure = () =>
      setSize({
        width: Math.max(240, host.clientWidth || 600),
        height: Math.max(200, host.clientHeight || 320),
      })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(host)
    return () => observer.disconnect()
  }, [])

  const view = useMemo(
    () =>
      selectSubgraph(
        { nodes: (graph?.nodes ?? []) as GraphNode[], edges: (graph?.edges ?? []) as GraphEdge[] },
        { scope, focus },
      ),
    [graph, scope, focus],
  )

  // The force layout is iterative: measured at 96ms average on the real
  // library (139 nodes, 200 edges). Run inline it fired on every parent
  // render, so with the scope set to "Whole library" each keystroke in the
  // send-back box blocked the main thread for a tenth of a second.
  const positions = useMemo(
    () => layout(view.nodes, view.edges, size.width, size.height),
    [view, size.width, size.height],
  )

  if (graph == null || graph.available === false) {
    return (
      <div ref={hostRef} className="flex h-full items-center justify-center text-sm text-muted-foreground">
        No graph source. The graph pane needs kb graph --json.
      </div>
    )
  }

  if (view.nodes.length === 0) {
    return (
      <div ref={hostRef} className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Nothing to draw yet. Distill a source and its notes will appear here.
      </div>
    )
  }

  return (
    <div ref={hostRef} className="h-full w-full">
      {/*
        `role="group"`, not `role="img"`: the img role is "children
        presentational", which prunes the per-node buttons below out of the
        accessibility tree. They stayed tab-focusable and announced nothing,
        so a keyboard user tabbed through 139 silent stops.
      */}
      <svg width={size.width} height={size.height} viewBox={`0 0 ${size.width} ${size.height}`} role="group" aria-label={`Link graph, ${view.nodes.length} notes, ${view.edges.length} links`} className="block">
        <g stroke="currentColor">
          {view.edges.map((edge, i) => {
            const a = positions.get(edge.source)
            const b = positions.get(edge.target)
            if (a === undefined || b === undefined) return null
            const inBatch = focus.has(edge.source) && focus.has(edge.target)
            return (
              <line
                key={i}
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                strokeOpacity={inBatch ? 0.35 : 0.12}
              >
                <title>{edge.reason ? `${edge.source} → ${edge.target}: ${edge.reason}` : `${edge.source} → ${edge.target}`}</title>
              </line>
            )
          })}
        </g>
        <g>
          {view.nodes.map((node, i) => {
            const point = positions.get(node.slug)
            if (point === undefined) return null
            const isCurrent = node.slug === selected
            const inBatch = focus.size === 0 || focus.has(node.slug)
            const radius = node.status === "unsettled" ? 5.5 : 4
            const flip = point.x > size.width * 0.62
            return (
              <g
                key={i}
                role="button"
                tabIndex={0}
                aria-label={node.title ?? node.slug}
                className="cursor-pointer"
                onClick={() => onSelect(node.slug)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault()
                    onSelect(node.slug)
                  }
                }}
              >
                <title>{`${node.title ?? node.slug}\n${node.slug}\n${node.status ?? ""}`}</title>
                {isCurrent ? (
                  <circle cx={point.x} cy={point.y} r={10} fill="none" stroke="currentColor" strokeOpacity={0.4} />
                ) : null}
                <circle
                  cx={point.x} cy={point.y} r={radius}
                  fill={inBatch ? "currentColor" : "none"}
                  stroke="currentColor"
                  strokeOpacity={inBatch ? 1 : 0.5}
                  fillOpacity={inBatch ? (isCurrent ? 1 : 0.85) : 0}
                />
                <text
                  x={flip ? point.x - 10 : point.x + 10}
                  y={point.y + 4}
                  textAnchor={flip ? "end" : "start"}
                  className="font-mono"
                  fontSize="10"
                  fill="currentColor"
                  fillOpacity={inBatch ? 0.85 : 0.5}
                >
                  {truncate(node.title ?? node.slug, 16)}
                </text>
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
