// The browser half's only route to the service.
// One module so every fetch shares the same error handling.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Json = any

async function request(path: string, options: RequestInit = {}): Promise<Json> {
  const response = await fetch(path, {
    headers: options.body === undefined ? {} : { "content-type": "application/json" },
    ...options,
  })
  const text = await response.text()
  let payload: Json = null
  try {
    payload = text === "" ? null : JSON.parse(text)
  } catch {
    throw new Error(`${path} returned something that is not JSON: ${text.slice(0, 200)}`)
  }
  if (!response.ok) {
    throw new Error(payload?.error ?? `${path} failed with ${response.status}`)
  }
  return payload
}

export const api = {
  health: () => request("/api/health"),
  config: () => request("/api/config"),
  queue: () => request("/api/queue"),
  note: (slug: string) => request(`/api/notes/${encodeURIComponent(slug)}`),
  doctor: () => request("/api/doctor"),
  graph: () => request("/api/graph"),
  runs: () => request("/api/runs"),
  run: (id: string) => request(`/api/runs/${encodeURIComponent(id)}`),
  review: (slug: string, action: string, comment?: string) =>
    request("/api/review", { method: "POST", body: JSON.stringify({ slug, action, ...(comment === undefined ? {} : { comment }) }) }),
  distill: (source: string, model?: string) =>
    request("/api/runs/distill", { method: "POST", body: JSON.stringify({ source, model }) }),
  settle: (model?: string) => request("/api/runs/settle", { method: "POST", body: JSON.stringify({ model }) }),
  rewrite: (model?: string) => request("/api/runs/rewrite", { method: "POST", body: JSON.stringify({ model }) }),
  stop: () => request("/api/runs/stop", { method: "POST", body: "{}" }),
  setBudget: (maxTurns: number, maxCostUsd: number) =>
    request("/api/budget", { method: "PUT", body: JSON.stringify({ maxTurns, maxCostUsd }) }),
}

// Live run telemetry over SSE (already in the browser, no dependency).
export function subscribeToRuns(onEvent: (event: Json) => void): () => void {
  const source = new EventSource("/api/events")
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data))
    } catch {
      // A malformed frame must not kill the stream.
    }
  }
  return () => source.close()
}
