/**
 * Resolve which Claude Code session owns this relay process.
 *
 * The speech server is shared by every session, so it cannot know which client
 * is calling. This relay is spawned per session by Claude Code, so it sits
 * underneath that session's own `claude` process and can read the name from the
 * same registry the fleet views already trust.
 *
 * The name is best-effort attribution metadata, not a required value. A miss
 * degrades to "no name" and the caller's own `sender` string is used instead.
 * That is a deliberate, scoped carve-out from AGENTS.md's "fail fast — never
 * swallow errors": a `say` must never fail because a label could not be
 * resolved. The carve-out is not silent — an unresolved name is logged at error
 * level — and the `say` path itself still propagates every request failure.
 */

import { execFileSync } from "child_process";
import { readFileSync } from "fs";
import { homedir } from "os";
import { basename, resolve } from "path";

import { log } from "./logger.js";

/** Where Claude Code keeps its session registry: `~/.claude/sessions/<pid>.json`. */
const SESSIONS_DIR = resolve(homedir(), ".claude", "sessions");

/**
 * `ps` costs a process spawn per hop, so bound the walk rather than trusting a
 * parent chain to terminate. Real chains here are two hops (`tsx` -> `claude`).
 */
const MAX_ANCESTOR_HOPS = 32;

/** Long enough for `ps` on a loaded machine, short enough that startup cannot stall. */
const PS_TIMEOUT_MS = 2_000;

/** The executable name of the Claude Code process. */
const CLAUDE_COMM = "claude";

export type SessionName = {
  /** The session's display name, as set by `/rename` or the session tag. */
  name: string;
  /** Where the name came from, mirroring the registry's own `nameSource`. */
  nameSource: string | null;
};

export type ProcessInfo = {
  comm: string;
  ppid: number;
};

/**
 * The two impure edges, injectable so the resolution can be tested without a
 * real process tree or a real `~/.claude` — the same shape `config.ts` uses for
 * its environment.
 */
export type SessionDeps = {
  readProcess: (pid: number) => ProcessInfo | null;
  readRegistryName: (claudePid: number) => SessionName | null;
};

/**
 * Read a process's executable name and parent pid.
 *
 * `comm` is returned verbatim — `ps -o comm=` reports a bare name for some
 * processes and an absolute path for others (verified on this machine —
 * `claude` comes back bare, `wezterm-gui` comes back as a full path), so the
 * ancestor match normalizes it through {@link isClaudeComm} rather than
 * assuming the bare form here.
 */
function readProcess(pid: number): ProcessInfo | null {
  try {
    const comm = execFileSync("ps", ["-p", String(pid), "-o", "comm="], {
      encoding: "utf8",
      timeout: PS_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    const ppid = execFileSync("ps", ["-p", String(pid), "-o", "ppid="], {
      encoding: "utf8",
      timeout: PS_TIMEOUT_MS,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();

    if (comm === "" || ppid === "") {
      return null;
    }
    const parent = Number(ppid);
    if (!Number.isInteger(parent) || parent < 0) {
      return null;
    }
    return { comm, ppid: parent };
  } catch {
    // A process that exits mid-walk is ordinary control flow, not a swallowed
    // error: `ps` exits non-zero for a pid that is already gone and the walk
    // simply ends there. The overall outcome is reported by the caller below.
    return null;
  }
}

/**
 * Read the session name Claude Code recorded for `claudePid`.
 *
 * Returns null when the file is absent, unreadable or malformed, and when it
 * carries no usable name — not every session has a registry entry (measured: a
 * shim under claude 83292 resolved to `name = None`).
 *
 * `sessionsDir` is injectable so the parsing can be exercised against a real
 * temporary registry rather than a stub of this function.
 */
export function readRegistryName(
  claudePid: number,
  sessionsDir: string = SESSIONS_DIR,
): SessionName | null {
  const path = resolve(sessionsDir, `${claudePid}.json`);
  try {
    const raw = readFileSync(path, "utf8");
    const parsed = JSON.parse(raw) as { name?: unknown; nameSource?: unknown };
    const name = typeof parsed.name === "string" ? parsed.name.trim() : "";
    if (name === "") {
      return null;
    }
    const nameSource =
      typeof parsed.nameSource === "string" ? parsed.nameSource : null;
    return { name, nameSource };
  } catch {
    return null;
  }
}

const defaultDeps: SessionDeps = { readProcess, readRegistryName };

/**
 * Whether a `ps -o comm=` value names the Claude Code process.
 *
 * Compared on the basename: `ps` reports a bare `claude` for the ancestor on
 * this machine, but an absolute path for other processes, so the match must not
 * assume the bare form. A pure predicate so the rule is testable directly
 * rather than only through a real process tree.
 */
export function isClaudeComm(comm: string): boolean {
  return basename(comm) === CLAUDE_COMM;
}

/**
 * Walk up from `pid` and return the first ancestor running Claude Code.
 *
 * Returns null when there is none within {@link MAX_ANCESTOR_HOPS}. The relay
 * can legitimately run outside a Claude session — a manual `npx tsx
 * mcp/tts-mcp.ts`, a test harness — and that is an expected absence, not a
 * failure.
 */
export function findClaudeAncestor(
  pid: number,
  readProcessFn: (pid: number) => ProcessInfo | null = readProcess,
): number | null {
  let current = pid;
  for (let hop = 0; hop < MAX_ANCESTOR_HOPS; hop += 1) {
    const info = readProcessFn(current);
    if (info === null) {
      return null;
    }
    if (isClaudeComm(info.comm)) {
      return current;
    }
    if (info.ppid <= 1) {
      return null;
    }
    current = info.ppid;
  }
  return null;
}

/**
 * The label to display: the resolved session name wins, the caller's value is
 * only a fallback.
 *
 * Precedence is the whole point — defaulting only when `sender` is *absent*
 * would change nothing, because the failure this fixes is a caller passing a
 * bad value, not omitting one.
 */
export function attributionLabel(
  session: SessionName | null,
  sender: string | null | undefined,
): string | null {
  return session?.name ?? sender ?? null;
}

/**
 * Resolve this process's Claude session name.
 *
 * Call this once at relay startup and hold the result: the answer cannot change
 * while the relay lives, and resolving per `say` would put two process spawns
 * and a file read on the latency path of every utterance.
 */
export function resolveSessionName(
  pid: number = process.pid,
  deps: SessionDeps = defaultDeps,
): SessionName | null {
  const claudePid = findClaudeAncestor(pid, deps.readProcess);
  if (claudePid === null) {
    log.error("session name unresolved", { reason: "no claude ancestor", pid });
    return null;
  }

  const session = deps.readRegistryName(claudePid);
  if (session === null) {
    log.error("session name unresolved", {
      reason: "no usable registry entry",
      claude_pid: claudePid,
    });
    return null;
  }

  log.info("session name resolved", {
    name: session.name,
    nameSource: session.nameSource,
    claude_pid: claudePid,
  });
  return session;
}
