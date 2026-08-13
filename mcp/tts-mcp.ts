/**
 * MCP server that relays TTS requests to the speech server.
 *
 * A transparent relay layer between MCP clients (Claude Code, Claude Desktop)
 * and the FastAPI TTS speech server. All responses are JSON. Errors from the
 * speech server are propagated as-is with full details — no retries, no
 * fallbacks, no swallowed errors.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { loadServerUrl } from "./config.js";
import { log } from "./logger.js";

const HEALTH_TIMEOUT_MS = 3_000;
const REQUEST_TIMEOUT_MS = 30_000;

type ToolResult = {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
};

async function healthCheck(): Promise<ToolResult | null> {
  const baseUrl = loadServerUrl();
  const url = `${baseUrl}/health`;
  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    if (!response.ok) {
      const error = {
        error: "health_check_failed",
        url,
        message: `Speech server health check failed: HTTP ${response.status}`,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
        isError: true,
      };
    }
    const body = await response.json() as { status?: string };
    if (body?.status !== "ok") {
      const error = {
        error: "health_check_failed",
        url,
        message: "Speech server reported unhealthy status",
        details: body,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
        isError: true,
      };
    }
    return null;
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    const error = {
      error: "health_check_unreachable",
      url,
      message: `Speech server is not reachable at ${baseUrl}`,
      details: detail,
    };
    log.error("health check unreachable", { url, details: detail });
    return {
      content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
      isError: true,
    };
  }
}

async function request(
  method: "GET" | "POST",
  path: string,
  body?: Record<string, unknown>,
): Promise<ToolResult> {
  const healthResult = await healthCheck();
  if (healthResult !== null) {
    return healthResult;
  }

  const baseUrl = loadServerUrl();
  const url = `${baseUrl}${path}`;

  let response: Response;
  try {
    const options: RequestInit = {
      method,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    };
    if (method === "POST" && body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    response = await fetch(url, options);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    const error = {
      error: "connection_failed",
      url,
      message: `Speech server is not reachable at ${baseUrl}`,
      details: detail,
    };
    log.error("request failed", { method, url, details: detail });
    return {
      content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
      isError: true,
    };
  }

  const responseText = await response.text();

  let responseBody: unknown;
  try {
    responseBody = JSON.parse(responseText);
  } catch {
    const error = {
      error: "invalid_json",
      status_code: response.status,
      url,
      raw_body: responseText.slice(0, 500),
    };
    return {
      content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
      isError: true,
    };
  }

  if (!response.ok) {
    const error = {
      error: "http_error",
      status_code: response.status,
      url,
      response: responseBody,
    };
    return {
      content: [{ type: "text", text: JSON.stringify(error, null, 2) }],
      isError: true,
    };
  }

  return {
    content: [{ type: "text", text: JSON.stringify(responseBody, null, 2) }],
  };
}

const server = new McpServer({
  name: "tts-mcp",
  version: "0.1.0",
});

server.tool(
  "say",
  "Queue text for speech synthesis and playback. Sends text to the TTS server which generates audio and plays it through speakers. Returns a message ID for status tracking.",
  {
    voice: z
      .string()
      .describe(
        "Voice to use for synthesis. Use get_voices to list available voices.",
      ),
    text: z.string().describe("Text to convert to speech."),
    instruct: z
      .string()
      .optional()
      .describe(
        "Optional emotion/style instruction, e.g. 'Very happy and excited.'. " +
          "Only supported by the qwen3 engine; requests pairing it with a " +
          "voxtral voice are rejected.",
      ),
    engine: z
      .string()
      .optional()
      .describe(
        "Engine to synthesize with, e.g. 'qwen3' or 'voxtral'. Omit to use " +
          "the server default. The voice must belong to this engine — see " +
          "get_voices for the per-engine grouping. The first request for an " +
          "engine loads its model, which can take ~15-20s; later requests are " +
          "immediate.",
      ),
  },
  async ({ voice, text, instruct, engine }) => {
    log.info("say", {
      voice,
      engine: engine ?? "default",
      instruct: instruct ?? null,
      text: text.slice(0, 80),
    });
    return request("POST", "/say", { text, voice, instruct, engine });
  },
);

server.tool(
  "cancel",
  "Stop speech that is currently playing and let the next queued message start immediately. " +
    "Call with no arguments when the user asks to skip, stop, or cut short what is being said. " +
    "Pass message_id to cancel one specific message (if it has not started yet it is dropped " +
    "without being synthesized at all), or all=true to stop the current message and drop the " +
    "whole queue behind it. Returns the cancelled message IDs and how many remain queued.",
  {
    message_id: z
      .string()
      .optional()
      .describe(
        "Message ID to cancel. Omit to cancel whatever is playing right now.",
      ),
    all: z
      .boolean()
      .optional()
      .describe(
        "Cancel the playing message and drop every queued message behind it.",
      ),
  },
  async ({ message_id, all }) => {
    log.info("cancel", { message_id: message_id ?? null, all: all ?? false });
    return request("POST", "/cancel", { message_id, all: all ?? false });
  },
);

server.tool(
  "get_voices",
  "List all available TTS voices and the default voice from the speech server. The response also groups voices per engine, with each engine's language, whether it supports instruct, and whether its model is already loaded.",
  {},
  async () => {
    log.info("get_voices");
    return request("GET", "/voices");
  },
);

server.tool(
  "get_status",
  "Check status of a speech synthesis request. Returns status (queued/loading/playing/completed/error), the engine used, original text, audio file path, and error details. 'loading' means the request is waiting on its engine's model to load.",
  {
    message_id: z
      .string()
      .describe("Message ID returned by the say tool."),
  },
  async ({ message_id }) => {
    log.info("get_status", { message_id });
    return request(
      "GET",
      `/status/${encodeURIComponent(message_id)}`,
    );
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
