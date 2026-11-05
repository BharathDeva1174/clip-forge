"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createIntegration,
  deleteIntegration,
  fetchModels,
  listIntegrations,
  listProviderPresets,
  removeApiKey,
  replaceApiKey,
  selectIntegration,
  testIntegration,
  updateIntegration,
  type ApiProtocol,
  type ConnectionStatus,
  type Integration,
  type ModelDescriptor,
  type PricingStatus,
  type ProviderPreset,
  type StructuredOutputMode,
} from "@/lib/api";

const PROTOCOL_LABELS: Record<string, string> = {
  openai_chat_completions: "OpenAI Chat Completions",
  openai_responses: "OpenAI Responses",
  anthropic_messages: "Anthropic Messages",
  google_generate_content: "Google Generate Content",
  system_one: "System One",
};

const PRICING_LABELS: Record<string, string> = {
  unknown: "Cost unknown",
  free: "Free",
  paid: "Paid",
  mixed: "Mixed",
};

const STATUS_LABELS: Record<ConnectionStatus, string> = {
  unknown: "Not tested",
  ok: "Connected",
  auth_failed: "Authentication failed",
  model_unavailable: "Model unavailable",
  protocol_mismatch: "Endpoint mismatch",
  rate_limited: "Rate limited",
  timeout: "Timed out",
  provider_error: "Provider error",
  unsupported: "Unsupported",
  invalid_configuration: "Not usable",
};

const STATUS_STYLES: Record<ConnectionStatus, string> = {
  unknown: "border-line-strong bg-surface text-ink-faint",
  ok: "border-ok/40 bg-ok/10 text-ok",
  auth_failed: "border-bad/40 bg-bad/10 text-bad",
  model_unavailable: "border-bad/40 bg-bad/10 text-bad",
  protocol_mismatch: "border-bad/40 bg-bad/10 text-bad",
  rate_limited: "border-warn/40 bg-warn/10 text-warn",
  timeout: "border-warn/40 bg-warn/10 text-warn",
  provider_error: "border-bad/40 bg-bad/10 text-bad",
  unsupported: "border-warn/40 bg-warn/10 text-warn",
  invalid_configuration: "border-warn/40 bg-warn/10 text-warn",
};

const FIELD_CLASS =
  "mt-1 w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-sm text-ink";

type FormState = {
  name: string;
  providerId: string;
  protocol: ApiProtocol;
  baseUrl: string;
  apiKey: string;
  modelId: string;
  modelName: string;
  contextWindow: string;
  maxOutputTokens: string;
  reasoningEffort: string;
  structuredOutput: StructuredOutputMode;
  pricingStatus: PricingStatus;
  localEndpoint: boolean;
};

const EMPTY_FORM: FormState = {
  name: "",
  providerId: "custom",
  protocol: "openai_chat_completions",
  baseUrl: "",
  apiKey: "",
  modelId: "",
  modelName: "",
  contextWindow: "",
  maxOutputTokens: "",
  reasoningEffort: "",
  structuredOutput: "none",
  pricingStatus: "unknown",
  localEndpoint: false,
};

/**
 * One form field.
 *
 * The label is wired to the control with `htmlFor`, and the help text is
 * wired with `aria-describedby`, so the control's accessible name is the
 * label alone and the help is still announced.
 */
function Field({
  id,
  label,
  help,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="block text-xs text-ink-muted">
      <label htmlFor={id}>{label}</label>
      {children}
      {help && (
        <p id={`${id}-help`} className="mt-1 block text-[11px] text-ink-faint">
          {help}
        </p>
      )}
    </div>
  );
}

export default function IntegrationsPanel() {
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<Integration | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [models, setModels] = useState<ModelDescriptor[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++latestRequest.current;
    const outcome = await listIntegrations().then(
      (saved) => ({ saved, failure: null as string | null }),
      (cause: unknown) => ({
        saved: null,
        failure:
          cause instanceof ApiError
            ? cause.message
            : "The integrations could not be loaded.",
      }),
    );
    if (requestId === latestRequest.current) {
      setIntegrations(outcome.saved ?? []);
      setError(outcome.failure);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // The state is set in the promise's callbacks rather than in the effect
    // body, so the render is not cascaded.
    let cancelled = false;
    void Promise.all([listProviderPresets(), listIntegrations()]).then(
      ([presetList, saved]) => {
        if (cancelled) {
          return;
        }
        setPresets(presetList);
        setIntegrations(saved);
        setError(null);
        setLoading(false);
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        setError(
          cause instanceof ApiError
            ? cause.message
            : "The integrations could not be loaded.",
        );
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const preset = useMemo(
    () => presets.find((item) => item.id === form.providerId),
    [presets, form.providerId],
  );

  const startAdding = () => {
    setEditing(null);
    setAdding(true);
    setModels([]);
    setModelsError(null);
    setFormError(null);
    setForm({ ...EMPTY_FORM });
  };

  const startEditing = (integration: Integration) => {
    setAdding(false);
    setEditing(integration);
    setModels([]);
    setModelsError(null);
    setFormError(null);
    setForm({
      name: integration.name,
      providerId: integration.provider_id,
      protocol: integration.protocol,
      baseUrl: integration.base_url,
      // The key is never sent back, so the field starts empty. Leaving it
      // empty keeps the stored key.
      apiKey: "",
      modelId: integration.model_id ?? "",
      modelName: integration.model_name ?? "",
      contextWindow:
        integration.context_window === null
          ? ""
          : String(integration.context_window),
      maxOutputTokens:
        integration.max_output_tokens === null
          ? ""
          : String(integration.max_output_tokens),
      reasoningEffort: integration.reasoning_effort ?? "",
      structuredOutput: integration.structured_output,
      pricingStatus: integration.pricing_status,
      localEndpoint: integration.local_endpoint,
    });
  };

  const cancel = () => {
    setAdding(false);
    setEditing(null);
    setFormError(null);
    setModels([]);
    setModelsError(null);
  };

  const chooseProvider = (providerId: string) => {
    const chosen = presets.find((item) => item.id === providerId);
    const protocol = chosen?.default_protocol ?? "openai_chat_completions";
    setForm((current) => ({
      ...current,
      providerId,
      protocol,
      baseUrl: chosen?.base_urls[protocol] ?? current.baseUrl,
    }));
  };

  const chooseProtocol = (protocol: ApiProtocol) => {
    setForm((current) => ({
      ...current,
      protocol,
      baseUrl:
        preset?.base_urls[protocol] ??
        (current.providerId === "custom" ? current.baseUrl : ""),
    }));
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        provider_id: form.providerId,
        protocol: form.protocol,
        base_url: form.baseUrl.trim(),
        model_id: form.modelId.trim() || null,
        model_name: form.modelName.trim() || null,
        context_window: form.contextWindow ? Number(form.contextWindow) : null,
        max_output_tokens: form.maxOutputTokens
          ? Number(form.maxOutputTokens)
          : null,
        reasoning_effort: form.reasoningEffort || null,
        structured_output: form.structuredOutput,
        pricing_status: form.pricingStatus,
        local_endpoint: form.localEndpoint,
      };

      if (editing) {
        await updateIntegration(editing.id, payload);
        // The key is replaced only when the user typed one.
        if (form.apiKey) {
          await replaceApiKey(editing.id, form.apiKey);
        }
        setNotice(`“${payload.name}” was saved.`);
      } else {
        const created = await createIntegration({
          ...payload,
          api_key: form.apiKey || undefined,
        });
        if (form.apiKey) {
          await replaceApiKey(created.id, form.apiKey);
        }
        setNotice(`“${payload.name}” was added.`);
      }
      cancel();
      await refresh();
    } catch (cause) {
      setFormError(
        cause instanceof ApiError ? cause.message : "The configuration was not saved.",
      );
    } finally {
      setSaving(false);
    }
  };

  const runAction = async (
    integration: Integration,
    action: (item: Integration) => Promise<unknown>,
    message: string,
  ) => {
    setBusyId(integration.id);
    setError(null);
    try {
      await action(integration);
      setNotice(message);
      await refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "That did not work.");
    } finally {
      setBusyId(null);
    }
  };

  const loadModels = async () => {
    setModelsError(null);
    setModels([]);
    if (!editing) {
      setModelsError(
        "Save the configuration first, then the provider's model list can be fetched.",
      );
      return;
    }
    try {
      const result = await fetchModels(editing.id);
      setModels(result.items);
      if (result.items.length === 0) {
        setModelsError("The provider returned no models.");
      }
    } catch (cause) {
      setModelsError(
        cause instanceof ApiError
          ? cause.message
          : "The model list could not be fetched.",
      );
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-ink">
            Integrations
          </h2>
          <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-ink-muted">
            Configure the AI providers ClipForge uses to choose highlight
            moments. Your API key is kept in this machine&apos;s credential
            store, never in the ClipForge database, and it is never sent to
            the browser.
          </p>
        </div>
        <button
          type="button"
          onClick={startAdding}
          className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
        >
          Add an integration
        </button>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-lg border border-bad/40 bg-bad/10 px-4 py-3 text-xs text-bad"
        >
          {error}
        </p>
      )}
      {notice && (
        <p
          role="status"
          className="rounded-lg border border-ok/40 bg-ok/10 px-4 py-3 text-xs text-ok"
        >
          {notice}
        </p>
      )}

      {loading && (
        <p className="text-sm text-ink-muted" role="status">
          Loading integrations…
        </p>
      )}

      {!loading && integrations.length === 0 && !adding && !editing && (
        <div className="rounded-2xl border border-dashed border-line-strong bg-surface px-6 py-10 text-center">
          <p className="text-sm font-medium text-ink">No integrations yet</p>
          <p className="mt-1 text-xs text-ink-muted">
            Add a provider to let ClipForge find the moments worth clipping.
          </p>
        </div>
      )}

      {integrations.length > 0 && (
        <ul className="space-y-3">
          {integrations.map((integration) => (
            <li
              key={integration.id}
              className="rounded-xl border border-line bg-surface px-4 py-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">
                    {integration.name}
                  </p>
                  <p className="mt-0.5 text-[11px] text-ink-faint">
                    {integration.provider_name} ·{" "}
                    {PROTOCOL_LABELS[integration.protocol] ??
                      integration.protocol}
                    {integration.model_id
                      ? ` · ${integration.model_id}`
                      : " · no model yet"}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {integration.is_default && (
                    <span className="rounded-full border border-ember-500/40 bg-ember-500/10 px-2.5 py-0.5 text-[11px] text-ember-300">
                      Used for highlights
                    </span>
                  )}
                  {!integration.enabled && (
                    <span className="rounded-full border border-line-strong bg-surface px-2.5 py-0.5 text-[11px] text-ink-faint">
                      Disabled
                    </span>
                  )}
                  {!integration.protocol_supported && (
                    <span className="rounded-full border border-warn/40 bg-warn/10 px-2.5 py-0.5 text-[11px] text-warn">
                      Protocol not supported
                    </span>
                  )}
                  <span
                    className={`rounded-full border px-2.5 py-0.5 text-[11px] ${STATUS_STYLES[integration.last_test_status]}`}
                  >
                    {STATUS_LABELS[integration.last_test_status]}
                  </span>
                  <span className="rounded-full border border-line-strong bg-surface px-2.5 py-0.5 text-[11px] text-ink-faint">
                    {PRICING_LABELS[integration.pricing_status]}
                  </span>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => startEditing(integration)}
                  className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
                >
                  Edit
                </button>
                <button
                  type="button"
                  disabled={busyId === integration.id}
                  onClick={() =>
                    runAction(
                      integration,
                      (item) => testIntegration(item.id),
                      "The connection was tested.",
                    )
                  }
                  className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
                >
                  Test connection
                </button>
                {!integration.is_default && (
                  <button
                    type="button"
                    disabled={busyId === integration.id}
                    onClick={() =>
                      runAction(
                        integration,
                        (item) => selectIntegration(item.id),
                        `“${integration.name}” is now used for highlight detection.`,
                      )
                    }
                    className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
                  >
                    Use for highlights
                  </button>
                )}
                <button
                  type="button"
                  disabled={busyId === integration.id}
                  onClick={() =>
                    runAction(
                      integration,
                      (item) =>
                        updateIntegration(item.id, { enabled: !item.enabled }),
                      integration.enabled
                        ? `“${integration.name}” was disabled.`
                        : `“${integration.name}” was enabled.`,
                    )
                  }
                  className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
                >
                  {integration.enabled ? "Disable" : "Enable"}
                </button>
                {integration.api_key_configured && (
                  <button
                    type="button"
                    disabled={busyId === integration.id}
                    onClick={() =>
                      runAction(
                        integration,
                        (item) => removeApiKey(item.id),
                        "The API key was removed.",
                      )
                    }
                    className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300 disabled:opacity-50"
                  >
                    Remove key
                  </button>
                )}
                <button
                  type="button"
                  disabled={busyId === integration.id}
                  onClick={() =>
                    runAction(
                      integration,
                      (item) => deleteIntegration(item.id),
                      `“${integration.name}” was removed.`,
                    )
                  }
                  className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-bad/60 hover:text-bad disabled:opacity-50"
                >
                  Delete
                </button>
              </div>

              {integration.last_tested_at && (
                <p className="mt-2 text-[11px] text-ink-faint">
                  Last tested{" "}
                  {new Date(integration.last_tested_at).toLocaleString()}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}

      {(adding || editing) && (
        <form
          onSubmit={save}
          className="rounded-2xl border border-line bg-surface p-5"
          aria-label="Integration configuration"
        >
          <h3 className="text-sm font-semibold text-ink">
            {editing ? `Edit “${editing.name}”` : "Add an integration"}
          </h3>

          {formError && (
            <p
              role="alert"
              className="mt-3 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad"
            >
              {formError}
            </p>
          )}

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Field id="integration-name" label="Name" help="A label for you. Only you see it.">
              <input
                id="integration-name"
                required
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder="My provider"
                className={FIELD_CLASS}
              />
            </Field>

            <Field id="integration-provider" label="Provider" help={preset?.summary}>
              <select
                id="integration-provider"
                value={form.providerId}
                onChange={(event) => chooseProvider(event.target.value)}
                className={FIELD_CLASS}
              >
                {presets.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              id="integration-protocol"
              label="API protocol"
              help="The shape of the request. Some providers speak more than one, and a model may need a different one than the provider's default."
            >
              <select
                id="integration-protocol"
                value={form.protocol}
                onChange={(event) =>
                  chooseProtocol(event.target.value as ApiProtocol)
                }
                className={FIELD_CLASS}
              >
                {Object.entries(PROTOCOL_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              id="integration-base-url"
              label="Base URL"
              help="Where the provider lives. A remote provider must use https."
            >
              <input
                id="integration-base-url"
                required
                value={form.baseUrl}
                onChange={(event) =>
                  setForm({ ...form, baseUrl: event.target.value })
                }
                placeholder="https://api.example.com/v1"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-api-key"
              label="API key"
              help="Saved in this machine's credential store. It is never returned to the browser, and leaving this empty keeps the key that is already saved."
            >
              <input
                id="integration-api-key"
                type="password"
                autoComplete="off"
                value={form.apiKey}
                onChange={(event) =>
                  setForm({ ...form, apiKey: event.target.value })
                }
                placeholder={
                  editing?.api_key_configured
                    ? "A key is saved — leave empty to keep it"
                    : "Optional for a local model server"
                }
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-model-id"
              label="Model ID"
              help="The exact identifier the provider expects. It is often not the same as the model's name."
            >
              <input
                id="integration-model-id"
                value={form.modelId}
                onChange={(event) =>
                  setForm({ ...form, modelId: event.target.value })
                }
                placeholder="llama-3.3-70b-versatile"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-model-name"
              label="Model name"
              help="A label to show. It is never sent to the provider."
            >
              <input
                id="integration-model-name"
                value={form.modelName}
                onChange={(event) =>
                  setForm({ ...form, modelName: event.target.value })
                }
                placeholder="Llama 3.3 70B"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-context-window"
              label="Context window"
              help="The whole context the model accepts, in tokens. Leave it empty if you do not know: ClipForge will not guess."
            >
              <input
                id="integration-context-window"
                type="number"
                min={1}
                value={form.contextWindow}
                onChange={(event) =>
                  setForm({ ...form, contextWindow: event.target.value })
                }
                placeholder="131072"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-max-output"
              label="Maximum output tokens"
              help="Optional. Only set it when the model has a limit."
            >
              <input
                id="integration-max-output"
                type="number"
                min={1}
                value={form.maxOutputTokens}
                onChange={(event) =>
                  setForm({ ...form, maxOutputTokens: event.target.value })
                }
                placeholder="4096"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-reasoning"
              label="Reasoning effort"
              help="Provider and model dependent. It is only sent when the model is known to accept it."
            >
              <input
                id="integration-reasoning"
                value={form.reasoningEffort}
                onChange={(event) =>
                  setForm({ ...form, reasoningEffort: event.target.value })
                }
                placeholder="medium"
                className={FIELD_CLASS}
              />
            </Field>

            <Field
              id="integration-structured-output"
              label="Structured output"
              help="How the model is asked for JSON. When it is not supported, the answer is validated locally instead."
            >
              <select
                id="integration-structured-output"
                value={form.structuredOutput}
                onChange={(event) =>
                  setForm({
                    ...form,
                    structuredOutput: event.target.value as StructuredOutputMode,
                  })
                }
                className={FIELD_CLASS}
              >
                <option value="none">Not supported</option>
                <option value="json_object">JSON object</option>
                <option value="json_schema">JSON schema</option>
              </select>
            </Field>

            <Field
              id="integration-pricing"
              label="Cost"
              help="What you know about the cost. Prices and free access are the provider's to change, so ClipForge does not track them."
            >
              <select
                id="integration-pricing"
                value={form.pricingStatus}
                onChange={(event) =>
                  setForm({
                    ...form,
                    pricingStatus: event.target.value as PricingStatus,
                  })
                }
                className={FIELD_CLASS}
              >
                <option value="unknown">Unknown</option>
                <option value="free">Free</option>
                <option value="paid">Paid</option>
                <option value="mixed">Mixed</option>
              </select>
            </Field>

            <div className="flex items-start gap-2 text-xs text-ink-muted sm:col-span-2">
              <input
                id="integration-local-endpoint"
                type="checkbox"
                checked={form.localEndpoint}
                onChange={(event) =>
                  setForm({ ...form, localEndpoint: event.target.checked })
                }
                className="mt-0.5"
              />
              <label htmlFor="integration-local-endpoint">
                This is a local endpoint on this machine.
                <span className="mt-1 block text-[11px] text-ink-faint">
                  Allows plain http and addresses on this machine. Leave it off
                  for a provider that runs somewhere else.
                </span>
              </label>
            </div>
          </div>

          <div className="mt-5 flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={saving}
              className="rounded-lg border border-ember-500/50 bg-ember-500/10 px-3 py-1.5 text-xs text-ember-300 transition-colors hover:border-ember-500 disabled:opacity-50"
            >
              {saving ? "Saving…" : editing ? "Save changes" : "Save"}
            </button>
            <button
              type="button"
              onClick={cancel}
              className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={loadModels}
              className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ember-300"
            >
              Fetch models
            </button>
          </div>

          {modelsError && (
            <p role="alert" className="mt-3 text-xs text-bad">
              {modelsError}
            </p>
          )}

          {models.length > 0 && (
            <div className="mt-4">
              <p className="text-xs text-ink-muted">
                Pick a model. The identifier is the provider&apos;s own.
              </p>
              <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto">
                {models.map((model) => (
                  <li key={model.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setForm((current) => ({
                          ...current,
                          modelId: model.id,
                          modelName: model.name ?? current.modelName,
                          contextWindow:
                            model.context_window === null
                              ? current.contextWindow
                              : String(model.context_window),
                          maxOutputTokens:
                            model.max_output_tokens === null
                              ? current.maxOutputTokens
                              : String(model.max_output_tokens),
                          structuredOutput:
                            model.structured_output === "none"
                              ? current.structuredOutput
                              : model.structured_output,
                        }));
                      }}
                      className="w-full rounded-lg border border-line px-3 py-2 text-left text-xs text-ink-muted transition-colors hover:border-ember-500 hover:text-ink"
                    >
                      <span className="font-mono">{model.id}</span>
                      {model.context_window && (
                        <span className="ml-2 text-ink-faint">
                          {model.context_window.toLocaleString()} tokens
                        </span>
                      )}
                      {model.protocol && (
                        <span className="ml-2 text-ink-faint">
                          {PROTOCOL_LABELS[model.protocol] ?? model.protocol}
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </form>
      )}
    </div>
  );
}
