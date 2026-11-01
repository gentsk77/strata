// The link graph layout. Pure, ported from the v0 graph.js so the SVG can be
// drawn 1:1 against the pane's real size (a scaled viewBox would multiply
// label font sizes with it).

/* eslint-disable @typescript-eslint/no-explicit-any */
export interface GraphNode { slug: string; title?: string; status?: string }
export interface GraphEdge { source: string; target: string; reason?: string }

export function selectSubgraph(
  graph: { nodes: GraphNode[]; edges: GraphEdge[] },
  opts: { scope: string; focus: Set<string> },
): { nodes: GraphNode[]; edges: GraphEdge[]; focus: Set<string> } {
  const nodes = graph.nodes ?? []
  const edges = graph.edges ?? []
  if (opts.scope === "all" || opts.focus.size === 0) {
    return { nodes, edges, focus: opts.focus }
  }
  const neighbours = new Set(opts.focus)
  for (const edge of edges) {
    if (opts.focus.has(edge.source)) neighbours.add(edge.target)
    if (opts.focus.has(edge.target)) neighbours.add(edge.source)
  }
  return {
    nodes: nodes.filter((node) => neighbours.has(node.slug)),
    edges: edges.filter((edge) => neighbours.has(edge.source) && neighbours.has(edge.target)),
    focus: opts.focus,
  }
}

function seeded(seed: number): () => number {
  let value = seed >>> 0
  return () => {
    value = (value * 1664525 + 1013904223) >>> 0
    return value / 4294967296
  }
}

export function layout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  width: number,
  height: number,
  iterations = 260,
): Map<string, { x: number; y: number }> {
  const random = seeded(nodes.length * 7919 + edges.length)
  const positions = new Map<string, { x: number; y: number }>()
  const centreX = width / 2
  const centreY = height / 2
  const radius = Math.min(width, height) * 0.36

  nodes.forEach((node, index) => {
    const angle = (index / Math.max(1, nodes.length)) * Math.PI * 2
    positions.set(node.slug, {
      x: centreX + Math.cos(angle) * radius * (0.7 + random() * 0.6),
      y: centreY + Math.sin(angle) * radius * (0.7 + random() * 0.6),
    })
  })

  const adjacency = edges
    .map((edge) => [positions.get(edge.source), positions.get(edge.target)])
    .filter((pair): pair is [{ x: number; y: number }, { x: number; y: number }] => pair[0] !== undefined && pair[1] !== undefined)

  const sideMargin = 64
  const edgeMargin = 20
  const clamp = (point: { x: number; y: number }): void => {
    point.x = Math.max(sideMargin, Math.min(width - sideMargin, point.x))
    point.y = Math.max(edgeMargin, Math.min(height - edgeMargin, point.y))
  }

  const ideal = Math.max(48, Math.min(width, height) / Math.sqrt(Math.max(2, nodes.length)))

  for (let step = 0; step < iterations; step += 1) {
    const cooling = 1 - step / iterations
    const all = [...positions.values()]
    for (let i = 0; i < all.length; i += 1) {
      for (let j = i + 1; j < all.length; j += 1) {
        const a = all[i]!
        const b = all[j]!
        let dx = a.x - b.x
        let dy = a.y - b.y
        let distance = Math.hypot(dx, dy)
        if (distance < 0.01) {
          dx = random() - 0.5
          dy = random() - 0.5
          distance = 0.01
        }
        const force = ((ideal * ideal) / distance) * 0.045 * cooling
        a.x += (dx / distance) * force
        a.y += (dy / distance) * force
        b.x -= (dx / distance) * force
        b.y -= (dy / distance) * force
      }
    }
    for (const [a, b] of adjacency) {
      const dx = b.x - a.x
      const dy = b.y - a.y
      const distance = Math.max(0.01, Math.hypot(dx, dy))
      const force = ((distance * distance) / ideal) * 0.008 * cooling
      a.x += (dx / distance) * force
      a.y += (dy / distance) * force
      b.x -= (dx / distance) * force
      b.y -= (dy / distance) * force
    }
    for (const point of all) {
      point.x += (centreX - point.x) * 0.004 * cooling
      point.y += (centreY - point.y) * 0.004 * cooling
      // Clamp every step, not once at the end. On a sparse graph repulsion
      // wins and points drift far outside the pane; clamping only at the end
      // stacks all of them on the same corner. Inside the box they keep
      // pushing each other apart along the edges instead.
      clamp(point)
    }
  }

  for (const point of positions.values()) clamp(point)
  return positions
}

export function truncate(text: any, max: number): string {
  const value = String(text ?? "")
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}
