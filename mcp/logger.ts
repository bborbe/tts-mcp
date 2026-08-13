/**
 * Structured logging for the MCP relay.
 *
 * stdout belongs to the MCP protocol — a stdio server speaks JSON-RPC frames on
 * it, so anything else written there corrupts the session. Every diagnostic
 * therefore goes to stderr, one JSON object per line, which is what a log
 * aggregator can parse and what plain `tail` still reads.
 */

type Fields = Record<string, unknown>;

type Level = "info" | "error";

function emit(level: Level, message: string, fields: Fields): void {
  const entry = {
    time: new Date().toISOString(),
    level,
    logger: "tts-mcp",
    message,
    ...fields,
  };
  process.stderr.write(`${JSON.stringify(entry)}\n`);
}

export const log = {
  info(message: string, fields: Fields = {}): void {
    emit("info", message, fields);
  },
  error(message: string, fields: Fields = {}): void {
    emit("error", message, fields);
  },
};
