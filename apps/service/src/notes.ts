import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

/**
 * The git blob hash of a note.
 *
 * Git's object id for a blob is `sha1("blob " + byteLength + "\0" + bytes)`.
 * Computed here rather than by shelling out to `git hash-object` once per note:
 * the queue hashes every note on every read, and 139 process spawns per request
 * is not a thing to ship.
 *
 * **CRLF is normalised to LF first, and that is not a detail.** The strata
 * checkout runs `core.autocrlf=true`, so a note on disk has CRLF line endings
 * while the blob git actually stored has LF. Hashing the bytes as they sit on
 * disk yields an id that matches nothing in git, verified against
 * `git ls-files -s`, which reports the LF hash. Normalising also gives the
 * behaviour the review key wants: re-checking out a note with different line
 * endings is not an edit, so it must not invalidate the maintainer's judgment.
 *
 * This is the second half of a review key. A note's review state is keyed by
 * slug plus this hash, so editing the note text invalidates the key and returns
 * the note to the queue (design doc section 3.2).
 */
export function blobHash(bytes: Buffer): string {
  const normalised = Buffer.from(bytes.toString('utf8').replace(/\r\n/g, '\n'), 'utf8');
  const header = Buffer.from(`blob ${normalised.byteLength}\0`, 'utf8');
  return createHash('sha1').update(header).update(normalised).digest('hex');
}

export interface NoteFile {
  readonly slug: string;
  readonly blob: string;
  readonly markdown: string;
  readonly frontMatter: Readonly<Record<string, string>>;
  /** Body with the front matter block removed, which is what the note pane renders. */
  readonly body: string;
}

/**
 * Reads one note's markdown from strata, read-only.
 *
 * Reading note text is explicitly permitted; deriving a view from it is not.
 * The link graph, the inventory and every other computed thing comes from `kb`
 * (design doc section 2). This function exists to render a document, nothing more.
 */
export async function readNote(notesDir: string, slug: string): Promise<NoteFile> {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
    throw new Error(`refusing to read a slug that is not a plain slug: ${slug}`);
  }
  const bytes = await readFile(join(notesDir, `${slug}.md`));
  const markdown = bytes.toString('utf8');
  const { frontMatter, body } = splitFrontMatter(markdown);
  return { slug, blob: blobHash(bytes), markdown, frontMatter, body };
}

/**
 * Splits a leading `---` fenced block off a note.
 *
 * A deliberately small reader: strata's front matter is flat `key: value` lines
 * and `spec/note-spec.md` is the law that keeps it that way. It is NOT a YAML
 * parser and must not grow into one. Anything structural about a note comes
 * from `kb`; this only recovers the handful of scalars the note pane displays.
 */
export function splitFrontMatter(markdown: string): {
  frontMatter: Record<string, string>;
  body: string;
} {
  const normalised = markdown.replace(/\r\n/g, '\n');
  if (!normalised.startsWith('---\n')) {
    return { frontMatter: {}, body: normalised };
  }
  const end = normalised.indexOf('\n---', 3);
  if (end === -1) {
    return { frontMatter: {}, body: normalised };
  }
  const block = normalised.slice(4, end);
  // `end` points at the newline before the closing `---`, so `end + 4` lands
  // just past the fence. What remains starts with the fence line's own newline
  // AND the blank line every note puts after the front matter, so strip the
  // whole leading run rather than one.
  const rest = normalised.slice(end + 4).replace(/^\n+/, '');

  const frontMatter: Record<string, string> = {};
  for (const line of block.split('\n')) {
    const colon = line.indexOf(':');
    if (colon <= 0) continue;
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).trim();
    if (key !== '') frontMatter[key] = value;
  }
  return { frontMatter, body: rest };
}

/** Every `[[wikilink]]` target in a body, in order, deduplicated. */
export function wikilinks(body: string): string[] {
  const found = new Set<string>();
  for (const match of body.matchAll(/\[\[([^\]|]+)(?:\|[^\]]*)?\]\]/g)) {
    const target = match[1]?.trim();
    if (target !== undefined && target !== '') found.add(target);
  }
  return [...found];
}
