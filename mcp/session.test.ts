import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

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

type Chain = Record<number, [string, number]>;

/** Build a `readProcess` stub from a pid -> [comm, ppid] map. */
function chainOf(links: Chain): (pid: number) => ProcessInfo | null {
  return (pid) => {
    const link = links[pid];
    return link === undefined ? null : { comm: link[0], ppid: link[1] };
  };
}

function depsFor(
  links: Chain,
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
const realChain: Chain = {
  95110: [
    "npm exec tsx /Users/bborbe/Documents/workspaces/tts-mcp/mcp/tts-mcp.ts",
    94037,
  ],
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
    assert.equal(isClaudeComm("claude"), true);
  });

  it("matches an absolute path to claude", () => {
    assert.equal(isClaudeComm("/usr/local/bin/claude"), true);
  });

  it("does not match a sibling binary whose name merely starts with claude", () => {
    assert.equal(isClaudeComm("/usr/local/bin/claude-helper"), false);
  });

  it("does not match a directory that happens to be named claude", () => {
    assert.equal(isClaudeComm("/opt/claude/bin/node"), false);
  });

  it("does not match other processes", () => {
    assert.equal(isClaudeComm("bash"), false);
    assert.equal(
      isClaudeComm("/Applications/WezTerm.app/Contents/MacOS/wezterm-gui"),
      false,
    );
  });
});

describe("findClaudeAncestor", () => {
  it("finds the claude ancestor two hops up the real chain", () => {
    assert.equal(findClaudeAncestor(95110, chainOf(realChain)), 94037);
  });

  it("matches a comm reported as an absolute path, not only a bare name", () => {
    // `ps -o comm=` returns a bare `claude` on this machine but a full path for
    // other processes, so the match must not assume the bare form.
    assert.equal(
      findClaudeAncestor(2, chainOf({ 2: ["tsx", 3], 3: ["/usr/local/bin/claude", 4], 4: ["launchd", 1] })),
      3,
    );
  });

  it("never examines pid 1, which is launchd rather than a session", () => {
    assert.equal(findClaudeAncestor(2, chainOf({ 2: ["tsx", 1], 1: ["claude", 0] })), null);
  });

  it("returns null when no ancestor is claude", () => {
    assert.equal(
      findClaudeAncestor(3, chainOf({ 3: ["tsx", 2], 2: ["bash", 1], 1: ["launchd", 0] })),
      null,
    );
  });

  it("returns null when the chain breaks mid-walk", () => {
    // The parent pid is gone — `ps` reports that as a non-zero exit.
    assert.equal(findClaudeAncestor(4, chainOf({ 4: ["tsx", 99] })), null);
  });

  it("terminates on a self-parenting pid instead of looping forever", () => {
    assert.equal(findClaudeAncestor(5, chainOf({ 5: ["tsx", 5] })), null);
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
    assert.deepEqual(readRegistryName(94037, dir), {
      name: "Fleet Manager",
      nameSource: "user",
    });
  });

  it("trims surrounding whitespace from the name", () => {
    const dir = registryDirWith({
      "1.json": JSON.stringify({ name: "  Manager Layer  ", nameSource: "user" }),
    });
    assert.deepEqual(readRegistryName(1, dir), {
      name: "Manager Layer",
      nameSource: "user",
    });
  });

  it("returns null for an empty name", () => {
    const dir = registryDirWith({
      "2.json": JSON.stringify({ name: "", nameSource: "user" }),
    });
    assert.equal(readRegistryName(2, dir), null);
  });

  it("returns null for a whitespace-only name", () => {
    const dir = registryDirWith({
      "3.json": JSON.stringify({ name: "   ", nameSource: "user" }),
    });
    assert.equal(readRegistryName(3, dir), null);
  });

  it("returns null for a missing name field", () => {
    const dir = registryDirWith({ "4.json": JSON.stringify({ pid: 4 }) });
    assert.equal(readRegistryName(4, dir), null);
  });

  it("returns null for a non-string name", () => {
    const dir = registryDirWith({ "5.json": JSON.stringify({ name: 42 }) });
    assert.equal(readRegistryName(5, dir), null);
  });

  it("returns a null nameSource when the field is absent, keeping the name", () => {
    const dir = registryDirWith({ "6.json": JSON.stringify({ name: "Solo" }) });
    assert.deepEqual(readRegistryName(6, dir), { name: "Solo", nameSource: null });
  });

  it("returns null for malformed JSON", () => {
    const dir = registryDirWith({ "7.json": "{ not json" });
    assert.equal(readRegistryName(7, dir), null);
  });

  it("returns null when the registry entry does not exist", () => {
    const dir = registryDirWith({});
    assert.equal(readRegistryName(83292, dir), null);
  });
});

describe("resolveSessionName", () => {
  it("resolves the session name and its source", () => {
    const deps = depsFor(realChain, {
      94037: { name: "Fleet Manager", nameSource: "user" },
    });
    assert.deepEqual(resolveSessionName(95110, deps), {
      name: "Fleet Manager",
      nameSource: "user",
    });
  });

  it("returns null when the claude ancestor has no registry entry", () => {
    // Measured on this machine: not every session has a registry file.
    assert.equal(resolveSessionName(95110, depsFor(realChain, {})), null);
  });

  it("returns null when there is no claude ancestor at all", () => {
    const links: Chain = { 7: ["tsx", 6], 6: ["bash", 1], 1: ["launchd", 0] };
    assert.equal(resolveSessionName(7, depsFor(links, {})), null);
  });

  it("does not throw when the process walk fails", () => {
    const deps: SessionDeps = {
      readProcess: () => null,
      readRegistryName: () => {
        throw new Error("must not be reached");
      },
    };
    assert.doesNotThrow(() => resolveSessionName(1, deps));
    assert.equal(resolveSessionName(1, deps), null);
  });
});

describe("attributionLabel", () => {
  const session: SessionName = { name: "Fleet Manager", nameSource: "user" };

  it("prefers the session name over a deliberately bad caller sender", () => {
    // The defect this fixes: a caller passing a poor value, not omitting one.
    assert.equal(
      attributionLabel(session, "worker-manager BRO-21546"),
      "Fleet Manager",
    );
  });

  it("prefers the session name when the caller passes nothing", () => {
    assert.equal(attributionLabel(session, null), "Fleet Manager");
    assert.equal(attributionLabel(session, undefined), "Fleet Manager");
  });

  it("falls back to the caller sender when no name resolved", () => {
    assert.equal(attributionLabel(null, "inbox triage"), "inbox triage");
  });

  it("falls back to null when neither is available", () => {
    assert.equal(attributionLabel(null, null), null);
    assert.equal(attributionLabel(null, undefined), null);
  });

  it("falls back to the caller sender when the caller passes an empty string", () => {
    // An empty string is falsy but not nullish — `??` keeps it, so this asserts
    // the documented behaviour rather than a surprise.
    assert.equal(attributionLabel(null, ""), "");
  });
});
