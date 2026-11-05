import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import IntegrationsPanel from "@/components/IntegrationsPanel";

const INTEGRATION = {
  id: "i".repeat(32),
  name: "My provider",
  provider_id: "groq",
  provider_name: "Groq",
  protocol: "openai_chat_completions",
  protocol_supported: true,
  base_url: "https://api.groq.com/openai/v1",
  model_id: "llama-3.3-70b-versatile",
  model_name: null,
  context_window: null,
  max_output_tokens: null,
  reasoning_effort: null,
  reasoning_effort_values: [],
  structured_output: "none",
  pricing_status: "unknown",
  local_endpoint: false,
  enabled: true,
  api_key_configured: true,
  is_default: false,
  role: "highlight_detection",
  last_tested_at: null,
  last_test_status: "unknown",
  last_test_code: null,
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:00:00+00:00",
};

const PRESETS = {
  items: [
    {
      id: "groq",
      name: "Groq",
      summary: "Fast inference on an OpenAI-compatible API.",
      default_protocol: "openai_chat_completions",
      base_urls: { openai_chat_completions: "https://api.groq.com/openai/v1" },
      models_url: "https://api.groq.com/openai/v1/models",
      supports_model_discovery: true,
      free_tier: "trial",
      free_tier_note: "The provider's limits and prices change.",
      docs_url: "https://console.groq.com/docs/overview",
    },
    {
      id: "custom",
      name: "Custom",
      summary: "Any endpoint that speaks one of the supported protocols.",
      default_protocol: "openai_chat_completions",
      base_urls: {},
      models_url: null,
      supports_model_discovery: false,
      free_tier: "unknown",
      free_tier_note: "Ask the provider what it costs.",
      docs_url: "",
    },
  ],
};

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Call = { url: string; method: string; body: unknown };

/** A fetch stub that answers the integration endpoints. */
function stubApi(overrides: Record<string, () => Response> = {}) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });

    for (const [fragment, answer] of Object.entries(overrides)) {
      if (url.includes(fragment)) {
        return answer();
      }
    }
    if (url.includes("/presets")) {
      return json(PRESETS);
    }
    if (url.includes("/integrations") && method === "GET") {
      return json({ items: [INTEGRATION], total: 1, limit: 50, offset: 0 });
    }
    if (url.includes("/integrations") && method === "POST") {
      return json(INTEGRATION, 201);
    }
    if (url.includes("/integrations") && method === "PATCH") {
      return json({ ...INTEGRATION, ...(body ?? {}) });
    }
    if (url.includes("/integrations") && method === "DELETE") {
      return new Response(null, { status: 204 });
    }
    return json({ error: { status: 404, code: "not_found", message: "no" } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("IntegrationsPanel", () => {
  it("shows an empty state when nothing is saved", async () => {
    stubApi({
      "/integrations": () => json({ items: [], total: 0, limit: 50, offset: 0 }),
    });

    render(<IntegrationsPanel />);

    await waitFor(() => expect(screen.getByText("No integrations yet")).toBeDefined());
  });

  it("lists the saved configurations", async () => {
    stubApi();

    render(<IntegrationsPanel />);

    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());
    expect(screen.getByText(/Groq/)).toBeDefined();
    expect(screen.getByText("Cost unknown")).toBeDefined();
  });

  it("marks the configuration that is used for highlights", async () => {
    stubApi({
      "/integrations": () =>
        json({
          items: [{ ...INTEGRATION, is_default: true }],
          total: 1,
          limit: 50,
          offset: 0,
        }),
    });

    render(<IntegrationsPanel />);

    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());
    expect(screen.getByText("Used for highlights")).toBeDefined();
    // The default is not offered a second time.
    expect(screen.queryByRole("button", { name: "Use for highlights" })).toBeNull();
  });

  it("never shows an API key", async () => {
    stubApi();

    render(<IntegrationsPanel />);

    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());
    expect(document.body.textContent).not.toContain("sk-");
    expect(document.body.textContent).not.toContain("api_key");
  });

  it("offers the provider presets and fills in the base URL", async () => {
    stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Add an integration" }));

    const provider = await screen.findByLabelText("Provider");
    expect(provider).toBeDefined();
    fireEvent.change(provider, { target: { value: "groq" } });

    const baseUrl = screen.getByLabelText("Base URL") as HTMLInputElement;
    expect(baseUrl.value).toBe("https://api.groq.com/openai/v1");
  });

  it("masks the API key field and keeps the saved key on an edit", async () => {
    stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const key = (await screen.findByLabelText("API key")) as HTMLInputElement;
    expect(key.type).toBe("password");
    // It starts empty: the saved key is never sent back to the browser.
    expect(key.value).toBe("");
    expect(key.placeholder).toContain("leave empty to keep it");
  });

  it("does not erase the key when the form is saved without one", async () => {
    const { calls } = stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const name = (await screen.findByLabelText("Name")) as HTMLInputElement;
    fireEvent.change(name, { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(screen.getByText(/was saved/)).toBeDefined());
    const keyCalls = calls.filter((call) => call.url.includes("/key"));
    expect(keyCalls).toHaveLength(0);
  });

  it("replaces the key when one is typed", async () => {
    const { calls } = stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const key = (await screen.findByLabelText("API key")) as HTMLInputElement;
    fireEvent.change(key, { target: { value: "a-new-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/key"))).toBe(true),
    );
    const keyCall = calls.find((call) => call.url.includes("/key"));
    expect(keyCall?.body).toEqual({ api_key: "a-new-key" });
  });

  it("reports a failed save without losing the form", async () => {
    stubApi({
      "/integrations": () =>
        json(
          {
            error: {
              status: 400,
              code: "ai_unsafe_url",
              message: "A remote provider must use https.",
            },
          },
          400,
        ),
    });

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("No integrations yet")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Add an integration" }));
    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: "Bad" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "http://api.example.test/v1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByText("A remote provider must use https.")).toBeDefined(),
    );
  });

  it("tests the connection and reports the outcome", async () => {
    const { calls } = stubApi({
      "/test": () =>
        json({
          status: "ok",
          code: "connection_ok",
          message: "The provider answered.",
          model: "llama-3.3-70b-versatile",
        }),
    });

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() =>
      expect(screen.getByText("The connection was tested.")).toBeDefined(),
    );
    expect(calls.some((call) => call.url.includes("/test"))).toBe(true);
  });

  it("shows a connection failure without leaking anything", async () => {
    stubApi({
      "/test": () =>
        json(
          {
            error: {
              status: 400,
              code: "ai_auth_failed",
              message: "The provider rejected the credentials.",
            },
          },
          400,
        ),
    });

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() =>
      expect(
        screen.getByText("The provider rejected the credentials."),
      ).toBeDefined(),
    );
  });

  it("fetches the model list and lets a model be chosen", async () => {
    stubApi({
      "/models": () =>
        json({
          items: [
            {
              id: "model-a",
              name: "Model A",
              context_window: 131072,
              max_output_tokens: null,
              structured_output: "none",
              available: true,
              protocol: "openai_chat_completions",
            },
          ],
          source_url: "https://api.groq.com/openai/v1/models",
          note: "This is what the provider advertises.",
        }),
    });

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Fetch models" }));

    const model = await screen.findByRole("button", { name: /model-a/ });
    fireEvent.click(model);

    const modelId = (await screen.findByLabelText("Model ID")) as HTMLInputElement;
    expect(modelId.value).toBe("model-a");
  });

  it("says when a model list cannot be fetched", async () => {
    stubApi({
      "/models": () =>
        json(
          {
            error: {
              status: 400,
              code: "ai_not_supported",
              message: "This provider does not publish a model list.",
            },
          },
          400,
        ),
    });

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Fetch models" }));

    await waitFor(() =>
      expect(
        screen.getByText("This provider does not publish a model list."),
      ).toBeDefined(),
    );
  });

  it("selects a configuration as the default", async () => {
    const { calls } = stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Use for highlights" }));

    await waitFor(() =>
      expect(screen.getByText(/is now used for highlight detection/)).toBeDefined(),
    );
    expect(calls.some((call) => call.url.includes("/select"))).toBe(true);
  });

  it("disables and deletes a configuration", async () => {
    const { calls } = stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(screen.getByText(/was disabled/)).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.getByText(/was removed/)).toBeDefined());
    expect(calls.some((call) => call.method === "DELETE")).toBe(true);
  });

  it("explains that a provider's cost can change", async () => {
    stubApi();

    render(<IntegrationsPanel />);
    await waitFor(() => expect(screen.getByText("My provider")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Add an integration" }));
    await screen.findByLabelText("Provider");

    expect(
      screen.getByText(/Prices and free access are the provider's to change/),
    ).toBeDefined();
  });
});
