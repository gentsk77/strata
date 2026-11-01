// Every status string in one place: the voice law says no view invents its own.
/* eslint-disable @typescript-eslint/no-explicit-any */

export const NOTE_STATUS = { unsettled: "Unsettled", settled: "Settled" }

export const REVIEW_STATUS = {
  approve: "Approved, waiting to settle",
  reject: "Sent back, waiting to rewrite",
  none: "Not reviewed",
  stale: "Text changed since review",
}

export const RUN_STATUS: Record<string, string> = {
  running: "Running",
  success: "Finished",
  error: "Stopped on an error",
  stopped: "Stopped",
}

export const RUN_KIND: Record<string, string> = {
  distill: "Distill",
  settle: "Settle",
  rewrite: "Rewrite",
}

export function formatCount(value: any): string {
  return Number(value ?? 0).toLocaleString("en-US")
}

export function formatCost(value: any): string {
  const amount = Number(value ?? 0)
  return amount === 0 ? "$0.00" : `$${amount < 0.01 ? amount.toFixed(4) : amount.toFixed(2)}`
}

export function formatDuration(ms: any): string {
  const seconds = Math.round(Number(ms ?? 0) / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`
}


export function willSettle(count: number): string {
  return count === 1 ? "1 note will settle" : `${count} notes will settle`
}

export function willRewrite(count: number): string {
  return count === 1 ? "1 note will be rewritten" : `${count} notes will be rewritten`
}
