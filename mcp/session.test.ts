import { mkdtempSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

import { describe, expect, it } from "vitest";

import {
  attributionLabel,
  findClaudeAncestor,
  isClaudeComm,
  readRegistryName,
  resolveSessionName,
  type ProcessInfo,
  type SessionDeps,
  type SessionName,
} from "./session.js";

/** Build a `readProcess` stub from a pid -> [comm, ppid] map. */
function chainOf(
  links: Record<number, [string, number]>,
): (pid: number) => ProcessInfo | null {
  return (pid) => {
    const link = links[pid];
    return link === undefined ? null : { comm: link[0], ppid: link[1] };
  };
}

function depsFor(
  links: Record<number, [string, number]>,
  registry: Record<number, SessionName | null>,
): SessionDeps {
  return {
    readProcess: chainOf(links),
    readRegistryName: (claudePid) => registry[claudePid] ?? null,
  };
}

/**
 * The real chain shape measured on this machine, where the relay runs as
 * `npm exec tsx <path>` and its parent is the session's `claude` process.
 */
const realChain: Record<number, [string, number]> = {
  95110: ["npm exec tsx /Users/bborbe/Documents/workspaces/tts-mcp/mcp/tts-mcp.ts", 94037],
  94037: ["claude", 93926],
  93926: ["bash", 45681],
  45681: ["/Applications/WezTerm.app/Contents/MacOS/wezterm-gui", 1],
};

/** Write a throwaway registry directory so the parser runs against real files. */
function registryDirWith(entries: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "tts-mcp-session-"));
  for (const [name, content] of Object.entries(entries)) {
    writeFileSync(join(dir, name), content, "utf8");
  }
  return dir;
}

describe("isClaudeComm", () => {
  it("matches a bare claude name, as ps reports it on this machine", () => {
    expect(isClaudeComm("claude")).toBe(true);
  });

  it("matches an absolute path to claude", () => {
    expect(isClaudeComm("/usr/local/bin/claude")).toBe(true);
  });

  it("does not match a sibling binary whose name merely starts with claude", () => {
    expect(isClaudeComm("/usr/local/bin/claude-helper")).toBe(false);
  });

  it("does not match a directory that happens to be named claude", () => {
    expect(isClaudeComm("/opt/claude/bin/node")).toBe(false);
  });

  it("does not match other processes", () => {
    expect(isClaudeComm("bash")).toBe(false);
    expect(
      isClaudeComm("/Applications/WezTerm.app/Contents/MacOS/wezterm-gui"),
    ).toBe(false);
  });
});

describe("findClaudeAncestor", () => {
  it("finds the claude ancestor two hops up the real chain", () => {
    expect(findClaudeAncestor(95110, chainOf(realChain))).toBe(94037);
  });

  it("matches a comm reported as an absolute path, not only a bare name", () => {
    // `ps -o comm=` returns a bare `claude` on this machine but a full path for
    // other processes, so the match must not assume the bare form. The chain
    // stops below pid 1 — the walk never examines launchd.
    const links = {
      2: ["tsx", 3],
      3: ["/usr/local/bin/claude", 4],
      4: ["launchd", 1],
    } as Record<number, [string, number]>;
    expect(findClaudeAncestor(2, chainOf(links))).toBe(3);
  });

  it("never examines pid 1, which is launchd rather than a session", () => {
    const links = { 2: ["tsx", 1], 1: ["claude", 0] } as Record<
      number,
      [string, number]
    >;
    expect(findClaudeAncestor(2, chainOf(links))).toBeNull();
  });

  it("returns null when no ancestor is claude", () => {
    const links = { 3: ["tsx", 2], 2: ["bash", 1], 1: ["launchd", 0] } as Record<
      number,
      [string, number]
    >;
    expect(findClaudeAncestor(3, chainOf(links))).toBeNull();
  });

  it("returns null when the chain breaks mid-walk", () => {
    // The parent pid is gone — `ps` reports that as a non-zero exit.
    expect(findClaudeAncestor(4, chainOf({ 4: ["tsx", 99] }))).toBeNull();
  });

  it("terminates on a self-parenting pid instead of looping forever", () => {
    expect(findClaudeAncestor(5, chainOf({ 5: ["tsx", 5] }))).toBeNull();
  });
});

describe("readRegistryName", () => {
  it("reads the name and its source from a real registry file", () => {
    const dir = registryDirWith({
      "94037.json": JSON.stringify({
        name: "Fleet Manager",
        nameSource: "user",
        pid: 94037,
      }),
    });
    expect(readRegistryName(94037, dir)).toEqual({
      name: "Fleet Manager",
      nameSource: "user",
    });
  });

  it("trims surrounding whitespace from the name", () => {
    const dir = registryDirWith({
      "1.json": JSON.stringify({ name: "  Manager Layer  ", nameSource: "user" }),
    });
    expect(readRegistryName(1, dir)).toEqual({
      name: "Manager Layer",
      nameSource: "user",
    });
  });

  it("returns null for an empty name", () => {
    const dir = registryDirWith({
      "2.json": JSON.stringify({ name: "", nameSource: "user" }),
    });
    expect(readRegistryName(2, dir)).toBeNull();
  });

  it("returns null for a whitespace-only name", () => {
    const dir = registryDirWith({
      "3.json": JSON.stringify({ name: "   ", nameSource: "user" }),
    });
    expect(readRegistryName(3, dir)).toBeNull();
  });

  it("returns null for a missing name field", () => {
    const dir = registryDirWith({ "4.json": JSON.stringify({ pid: 4 }) });
    expect(readRegistryName(4, dir)).toBeNull();
  });

  it("returns null for a non-string name", () => {
    const dir = registryDirWith({ "5.json": JSON.stringify({ name: 42 }) });
    expect(readRegistryName(5, dir)).toBeNull();
  });

  it("returns null when nameSource is absent, keeping the name", () => {
    const dir = registryDirWith({ "6.json": JSON.stringify({ name: "Solo" }) });
    expect(readRegistryName(6, dir)).toEqual({ name: "Solo", nameSource: null });
  });

  it("returns null for malformed JSON", () => {
    const dir = registryDirWith({ "7.json": "{ not json" });
    expect(readRegistryName(7, dir)).toBeNull();
  });

  it("returns null when the registry entry does not exist", () => {
    const dir = registryDirWith({});
    expect(readRegistryName(83292, dir)).toBeNull();
  });
});

describe("resolveSessionName", () => {
  it("resolves the session name and its source", () => {
    const deps = depsFor(realChain, {
      94037: { name: "Fleet Manager", nameSource: "user" },
    });
    expect(resolveSessionName(95110, deps)).toEqual({
      name: "Fleet Manager",
      nameSource: "user",
    });
  });

  it("returns null when the claude ancestor has no registry entry", () => {
    // Measured on this machine: not every session has a registry file.
    expect(resolveSessionName(95110, depsFor(realChain, {}))).toBeNull();
  });

  it("returns null when there is no claude ancestor at all", () => {
    const links = { 7: ["tsx", 6], 6: ["bash", 1], 1: ["launchd", 0] } as Record<
      number,
      [string, number]
    >;
    expect(resolveSessionName(7, depsFor(links, {}))).toBeNull();
  });

  it("does not throw when the process walk fails", () => {
    const deps: SessionDeps = {
      readProcess: () => null,
      readRegistryName: () => {
        throw new Error("must not be reached");
      },
    };
    expect(() => resolveSessionName(1, deps)).not.toThrow();
    expect(resolveSessionName(1, deps)).toBeNull();
  });
});

describe("attributionLabel", () => {
  const session: SessionName = { name: "Fleet Manager", nameSource: "user" };

  it("prefers the session name over a deliberately bad caller sender", () => {
    // The defect this fixes: a caller passing a poor value, not omitting one.
    expect(attributionLabel(session, "worker-manager BRO-21546")).toBe(
      "Fleet Manager",
    );
  });

  it("prefers the session name when the caller passes nothing", () => {
    expect(attributionLabel(session, null)).toBe("Fleet Manager");
    expect(attributionLabel(session, undefined)).toBe("Fleet Manager");
  });

  it("falls back to the caller sender when no name resolved", () => {
    expect(attributionLabel(null, "inbox triage")).toBe("inbox triage");
  });

  it("falls back to null when neither is available", () => {
    expect(attributionLabel(null, null)).toBeNull();
    expect(attributionLabel(null, undefined)).toBeNull();
  });

  it("falls back to the caller sender when the caller passes an empty string", () => {
    // An empty string is falsy but not nullish — `??` keeps it, so this asserts
    // the documented behaviour rather than a surprise.
    expect(attributionLabel(null, "")).toBe("");
  });
});
