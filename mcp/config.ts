/**
 * Configuration for the MCP relay — the one place that reads the environment.
 *
 * Nothing here runs at import time: an env read at module scope is evaluated
 * before anything can validate it, so a missing or malformed value surfaces as
 * a confusing failure somewhere else entirely. Every value is resolved on
 * demand, and a missing required key raises immediately with the file it was
 * expected in.
 */

import { existsSync, readFileSync } from "fs";
import { homedir } from "os";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** The environment variables this relay reads. */
export type Environment = {
  TTS_MCP_CONFIG?: string | undefined;
  XDG_CONFIG_HOME?: string | undefined;
};

/**
 * Resolve the config file, matching src/tts/config.py resolve_config_path().
 *
 * Precedence:
 *   1. $TTS_MCP_CONFIG (explicit override)
 *   2. $XDG_CONFIG_HOME/tts-mcp/config.yaml (defaults to ~/.config/tts-mcp/config.yaml)
 *   3. <project root>/config.yaml (back-compat)
 */
export function resolveConfigPath(env: Environment = process.env): string {
  const override = env.TTS_MCP_CONFIG;
  if (override) {
    return override;
  }
  const xdgHome = env.XDG_CONFIG_HOME || resolve(homedir(), ".config");
  const xdgPath = resolve(xdgHome, "tts-mcp", "config.yaml");
  if (existsSync(xdgPath)) {
    return xdgPath;
  }
  return resolve(PROJECT_ROOT, "config.yaml");
}

function readRequired(content: string, key: string, pattern: RegExp, configPath: string): string {
  const match = content.match(pattern);
  if (!match) {
    throw new Error(`Missing required key '${key}' in ${configPath}`);
  }
  return match[1].trim();
}

/**
 * Build the base URL of the speech server from the config file.
 *
 * Throws if the file cannot be read or either required key is absent — the
 * relay has no useful behavior without a server to talk to, so guessing a
 * host or port here would only turn a clear error into a silent one.
 */
export function loadServerUrl(env: Environment = process.env): string {
  const configPath = resolveConfigPath(env);

  let content: string;
  try {
    content = readFileSync(configPath, "utf-8");
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    throw new Error(`Cannot read config file ${configPath}: ${detail}`);
  }

  const host = readRequired(content, "host", /^host:\s*(.+)$/m, configPath);
  const port = readRequired(content, "port", /^port:\s*(\d+)/m, configPath);

  // 0.0.0.0 is a listen address; connect via 127.0.0.1
  const connectHost = host === "0.0.0.0" ? "127.0.0.1" : host;

  return `http://${connectHost}:${port}`;
}
