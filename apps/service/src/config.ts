/**
 * Resolves the port to listen on.
 *
 * `BENCH_SERVICE_PORT` wins when it is a usable port number. Anything else
 * (unset, empty, non-numeric, out of range) falls back to the default rather
 * than throwing: a typo in an env var should not stop the bench from coming up
 * on its documented port.
 */
export function resolvePort(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw.trim() === '') {
    return fallback;
  }

  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 65535) {
    return fallback;
  }

  return parsed;
}
