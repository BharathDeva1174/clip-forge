import { describe, expect, it, vi } from "vitest";

import { fetchBackendHealth, type FetchLike } from "@/lib/health";

const BASE_URL = "http://localhost:8000";

function stubFetch(response: unknown): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => response);
  return fetchMock;
}

describe("fetchBackendHealth", () => {
  it("reports an online backend", async () => {
    const fetchMock = stubFetch({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok", service: "clipforge-api" }),
    });

    const result = await fetchBackendHealth(
      BASE_URL,
      fetchMock as unknown as FetchLike,
    );

    expect(result).toEqual({ state: "online", service: "clipforge-api" });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/health",
      expect.objectContaining({
        headers: { accept: "application/json" },
      }),
    );
  });

  it("reports an offline backend when the request fails", async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    const result = await fetchBackendHealth(
      BASE_URL,
      fetchMock as unknown as FetchLike,
    );

    expect(result.state).toBe("offline");
    expect(result.state === "offline" && result.reason).toContain(BASE_URL);
  });

  it("reports an offline backend for a non-2xx response", async () => {
    const fetchMock = stubFetch({
      ok: false,
      status: 503,
      json: async () => ({}),
    });

    const result = await fetchBackendHealth(
      BASE_URL,
      fetchMock as unknown as FetchLike,
    );

    expect(result).toEqual({
      state: "offline",
      reason: "The backend answered with HTTP 503.",
    });
  });

  it("reports an offline backend for an unexpected payload", async () => {
    const fetchMock = stubFetch({
      ok: true,
      status: 200,
      json: async () => ({ unexpected: true }),
    });

    const result = await fetchBackendHealth(
      BASE_URL,
      fetchMock as unknown as FetchLike,
    );

    expect(result.state).toBe("offline");
  });

  it("strips trailing slashes from the base URL", async () => {
    const fetchMock = stubFetch({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok", service: "clipforge-api" }),
    });

    await fetchBackendHealth(
      "http://localhost:8000/",
      fetchMock as unknown as FetchLike,
    );

    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/health");
  });
});
