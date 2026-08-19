import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";

type Provider = {
  id: string;
  label: string;
  api_key_url?: string;
  show_api_key: boolean;
  show_base_url: boolean;
  default_model: string;
  default_base_url: string;
  extra_fields: { suffix: string; label: string; secret?: boolean }[];
};

type Options = { providers: Provider[] };

type Settings = {
  app: Record<string, unknown>;
  seedance: Record<string, unknown>;
  azure: Record<string, unknown>;
  siliconflow: Record<string, unknown>;
  elevenlabs: Record<string, unknown>;
  chatterbox: Record<string, unknown>;
  ui: Record<string, unknown>;
};

function keysToText(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join("\n");
  return String(value || "");
}

function textToKeys(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const options = useQuery({
    queryKey: ["workspace-options"],
    queryFn: () => api.get<Options>("/api/v1/workspace/options"),
  });
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/api/v1/settings"),
  });
  const [form, setForm] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.put<Settings>("/api/v1/settings", body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["settings"] }),
  });
  const providerId = form.llm_provider || String(query.data?.app.llm_provider || "moonshot");
  const provider = useMemo(
    () => options.data?.providers.find((item) => item.id === providerId),
    [options.data, providerId],
  );

  useEffect(() => {
    if (!query.data) return;
    const app = query.data.app;
    setForm({
      llm_provider: String(app.llm_provider || "moonshot"),
      api_key: String(app[`${app.llm_provider || "moonshot"}_api_key`] || ""),
      base_url: String(app[`${app.llm_provider || "moonshot"}_base_url`] || ""),
      model_name: String(app[`${app.llm_provider || "moonshot"}_model_name`] || ""),
      pexels_api_keys: keysToText(app.pexels_api_keys),
      pixabay_api_keys: keysToText(app.pixabay_api_keys),
      coverr_api_keys: keysToText(app.coverr_api_keys),
      sonilo_api_key: String(app.sonilo_api_key || ""),
      seedance_api_key: String(query.data.seedance.api_key || ""),
      seedance_model_id: String(query.data.seedance.model_id || ""),
      azure_speech_key: String(query.data.azure.speech_key || ""),
      azure_speech_region: String(query.data.azure.speech_region || ""),
      siliconflow_api_key: String(query.data.siliconflow.api_key || ""),
      elevenlabs_api_key: String(query.data.elevenlabs.api_key || ""),
      mimo_api_key: String(app.mimo_api_key || ""),
    });
  }, [query.data]);

  useEffect(() => {
    if (!query.data) return;
    const app = query.data.app;
    setForm((current) => {
      const extras: Record<string, string> = {};
      for (const field of provider?.extra_fields || []) {
        extras[`extra_${field.suffix}`] = String(app[`${providerId}_${field.suffix}`] || "");
      }
      return {
        ...current,
        api_key: String(app[`${providerId}_api_key`] || ""),
        base_url: String(app[`${providerId}_base_url`] || ""),
        model_name: String(app[`${providerId}_model_name`] || ""),
        ...extras,
      };
    });
  }, [provider, providerId, query.data]);

  if (!query.data) {
    return <section className="page">{query.error?.message || "正在读取配置"}</section>;
  }

  const set = (key: string, value: string) => setForm((current) => ({ ...current, [key]: value }));

  return (
    <section className="page">
      <form
        className="card grid"
        onSubmit={(event) => {
          event.preventDefault();
          const current = providerId;
          const app: Record<string, unknown> = {
            ...(query.data.app || {}),
            llm_provider: current,
            pexels_api_keys: textToKeys(form.pexels_api_keys || ""),
            pixabay_api_keys: textToKeys(form.pixabay_api_keys || ""),
            coverr_api_keys: textToKeys(form.coverr_api_keys || ""),
            sonilo_api_key: form.sonilo_api_key || "",
            mimo_api_key: form.mimo_api_key || "",
          };
          if (provider?.show_api_key) app[`${current}_api_key`] = form.api_key || "";
          if (provider?.show_base_url) app[`${current}_base_url`] = form.base_url || "";
          app[`${current}_model_name`] = form.model_name || "";
          for (const field of provider?.extra_fields || []) {
            app[`${current}_${field.suffix}`] = form[`extra_${field.suffix}`] || "";
          }
          void mutation.mutateAsync({
            app,
            seedance: {
              ...(query.data.seedance || {}),
              api_key: form.seedance_api_key || "",
              model_id: form.seedance_model_id || "",
            },
            azure: {
              ...(query.data.azure || {}),
              speech_key: form.azure_speech_key || "",
              speech_region: form.azure_speech_region || "",
            },
            siliconflow: { ...(query.data.siliconflow || {}), api_key: form.siliconflow_api_key || "" },
            elevenlabs: { ...(query.data.elevenlabs || {}), api_key: form.elevenlabs_api_key || "" },
          });
        }}
      >
        <label>
          大模型
          <select value={providerId} onChange={(event) => set("llm_provider", event.target.value)}>
            {(options.data?.providers || []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        {provider?.show_api_key ? (
          <label>
            API Key
            {provider.api_key_url ? (
              <a href={provider.api_key_url} target="_blank" rel="noreferrer">
                申请
              </a>
            ) : null}
            <input
              type="password"
              value={form.api_key || ""}
              onChange={(event) => set("api_key", event.target.value)}
            />
          </label>
        ) : null}
        {provider?.show_base_url ? (
          <label>
            Base URL
            <input
              value={form.base_url || ""}
              placeholder={provider.default_base_url}
              onChange={(event) => set("base_url", event.target.value)}
            />
          </label>
        ) : null}
        <label>
          模型名称
          <input
            value={form.model_name || ""}
            placeholder={provider?.default_model || ""}
            onChange={(event) => set("model_name", event.target.value)}
          />
        </label>
        {(provider?.extra_fields || []).map((field) => (
          <label key={field.suffix}>
            {field.label}
            <input
              type={field.secret ? "password" : "text"}
              value={form[`extra_${field.suffix}`] || ""}
              onChange={(event) => set(`extra_${field.suffix}`, event.target.value)}
            />
          </label>
        ))}

        <details open>
          <summary>素材接口</summary>
          <div className="grid">
            <label>
              Pexels 密钥
              <textarea
                value={form.pexels_api_keys || ""}
                onChange={(event) => set("pexels_api_keys", event.target.value)}
              />
            </label>
            <label>
              Pixabay 密钥
              <textarea
                value={form.pixabay_api_keys || ""}
                onChange={(event) => set("pixabay_api_keys", event.target.value)}
              />
            </label>
            <label>
              Coverr 密钥
              <textarea
                value={form.coverr_api_keys || ""}
                onChange={(event) => set("coverr_api_keys", event.target.value)}
              />
            </label>
            <label>
              Sonilo 密钥
              <input
                type="password"
                value={form.sonilo_api_key || ""}
                onChange={(event) => set("sonilo_api_key", event.target.value)}
              />
            </label>
          </div>
        </details>

        <details open>
          <summary>分镜生成</summary>
          <div className="grid two">
            <label>
              Seedance 密钥
              <input
                type="password"
                value={form.seedance_api_key || ""}
                onChange={(event) => set("seedance_api_key", event.target.value)}
              />
            </label>
            <label>
              Seedance 模型
              <input
                value={form.seedance_model_id || ""}
                onChange={(event) => set("seedance_model_id", event.target.value)}
              />
            </label>
          </div>
        </details>

        <details>
          <summary>语音服务</summary>
          <div className="grid two">
            <label>
              Azure 语音密钥
              <input
                type="password"
                value={form.azure_speech_key || ""}
                onChange={(event) => set("azure_speech_key", event.target.value)}
              />
            </label>
            <label>
              Azure 区域
              <input
                value={form.azure_speech_region || ""}
                onChange={(event) => set("azure_speech_region", event.target.value)}
              />
            </label>
            <label>
              硅基流动密钥
              <input
                type="password"
                value={form.siliconflow_api_key || ""}
                onChange={(event) => set("siliconflow_api_key", event.target.value)}
              />
            </label>
            <label>
              ElevenLabs 密钥
              <input
                type="password"
                value={form.elevenlabs_api_key || ""}
                onChange={(event) => set("elevenlabs_api_key", event.target.value)}
              />
            </label>
            <label>
              小米 MiMo 密钥
              <input
                type="password"
                value={form.mimo_api_key || ""}
                onChange={(event) => set("mimo_api_key", event.target.value)}
              />
            </label>
          </div>
        </details>

        <button className="primary" type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "正在保存" : "保存配置"}
        </button>
        {mutation.error ? <div className="error">{mutation.error.message}</div> : null}
      </form>
    </section>
  );
}
