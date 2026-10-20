import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import HealthIndicator from "@/components/HealthIndicator";

const BASE_URL = "http://localhost:8000";

const HEALTH_PAYLOAD = { status: "ok", service: "clipforge-api" };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubFetch(implementation: () => Promise<unknown>) {
  vi.stubGlobal("fetch", vi.fn(implementation));
}

describe("HealthIndicator", () => {
  it("shows a checking state while the request is in flight", async () => {
    let resolveFetch: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    stubFetch(() => pending);

    render(<HealthIndicator baseUrl={BASE_URL} />);

    expect(screen.getByText("Checking backend…")).toBeDefined();

    resolveFetch({
      ok: true,
      status: 200,
      json: async () => HEALTH_PAYLOAD,
    });

    await waitFor(() => expect(screen.getByText("Backend online")).toBeDefined());
  });

  it("reports an online backend", async () => {
    stubFetch(async () => ({
      ok: true,
      status: 200,
      json: async () => HEALTH_PAYLOAD,
    }));

    render(<HealthIndicator baseUrl={BASE_URL} />);

    await waitFor(() => expect(screen.getByText("Backend online")).toBeDefined());
    expect(screen.getByText("clipforge-api")).toBeDefined();
  });

  it("reports an unreachable backend and recovers when the retry succeeds", async () => {
    let attempt = 0;
    stubFetch(async () => {
      attempt += 1;
      if (attempt === 1) {
        throw new TypeError("Failed to fetch");
      }
      return { ok: true, status: 200, json: async () => HEALTH_PAYLOAD };
    });

    render(<HealthIndicator baseUrl={BASE_URL} />);

    await waitFor(() =>
      expect(screen.getByText("Backend unreachable")).toBeDefined(),
    );
    expect(screen.getByText(/Could not reach the backend/)).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getByText("Backend online")).toBeDefined());
  });
});
