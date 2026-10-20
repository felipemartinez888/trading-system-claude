import { beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, setToken, TOKEN } from "../test/fixtures";
import { ApiError, TOKEN_HEADER, api, liveUrl, readToken } from "./client";

describe("api client", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("reads the token injected by the backend", () => {
    expect(readToken()).toBeNull();
    setToken();
    expect(readToken()).toBe(TOKEN);
  });

  it.each([
    ["startMonitoring", "/api/monitoring/start"],
    ["stopMonitoring", "/api/monitoring/stop"],
    ["quit", "/api/app/quit"],
  ] as const)("%s sends POST %s with the token header", async (method, path) => {
    setToken();
    const mock = vi.fn<typeof fetch>(async () => jsonResponse({}));
    vi.stubGlobal("fetch", mock);
    await api[method]();
    const [url, init] = mock.mock.calls[0]!;
    expect(url).toBe(path);
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get(TOKEN_HEADER)).toBe(TOKEN);
  });

  it("does not send the token on reads", async () => {
    setToken();
    const mock = vi.fn<typeof fetch>(async () => jsonResponse({}));
    vi.stubGlobal("fetch", mock);
    await api.status();
    const [, init] = mock.mock.calls[0]!;
    expect(new Headers(init?.headers).get(TOKEN_HEADER)).toBeNull();
  });

  it("surfaces the API error detail", async () => {
    setToken();
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => jsonResponse({ detail: "monitoring already running" }, 409)),
    );
    const error: unknown = await api.startMonitoring().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, detail: "monitoring already running" });
  });

  it("builds the WebSocket URL on the same host", () => {
    const url = liveUrl("a b", { protocol: "http:", host: "127.0.0.1:8765" } as Location);
    expect(url).toBe("ws://127.0.0.1:8765/ws/live?token=a%20b");
  });
});
