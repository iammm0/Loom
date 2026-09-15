import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";

type Provider = {
  id: string;
  label: string;
  show_api_key: boolean;
  show_base_url: boolean;
  default_model: string;
  default_base_url: string;
  api_key_url?: string;
  extra_fields: { suffix: string; label: string; secret?: boolean }[];
};

type Options = {
  providers: Provider[];
  seedance_models?: { id: string; label: string }[];
};

type ModelOption = { id: string; label: string };

type Settings = {
  app: Record<string, unknown>;
  seedance: Record<string, unknown>;
  ui: Record<string, unknown>;
};

type TabId = "seedance" | "materials" | "llm" | "tts";

const STOCK_SOURCES: { id: string; label: string }[] = [
  { id: "local", label: "本地素材库" },
  { id: "pexels", label: "Pexels" },
  { id: "pixabay", label: "Pixabay" },
  { id: "coverr", label: "Coverr" },
];

function parseSources(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  return ["local", "pexels", "pixabay", "coverr"];
}

function toggleSource(current: string[], id: string): string[] {
  if (current.includes(id)) {
    const next = current.filter((item) => item !== id);
    return next.length ? next : current;
  }
  return STOCK_SOURCES.map((item) => item.id).filter((item) => item === id || current.includes(item));
}

const TABS: { id: TabId; label: string }[] = [
  { id: "seedance", label: "视频生成模型" },
  { id: "materials", label: "视频素材与素材库" },
  { id: "llm", label: "文案生成 LLM" },
  { id: "tts", label: "TTS 语音合成" },
];

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

function Field({
  label,
  name,
  hint,
  children,
}: {
  label: string;
  name?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="settings-field">
      <span className="settings-label">
        {label}
        {name ? <code className="sheet-param">{name}</code> : null}
      </span>
      {children}
      {hint ? <span className="settings-hint">{hint}</span> : null}
    </label>
  );
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabId>("seedance");
  const [form, setForm] = useState<Record<string, string>>({});
  const [savedAt, setSavedAt] = useState("");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [probeError, setProbeError] = useState("");
  const [probing, setProbing] = useState(false);

  const options = useQuery({
    queryKey: ["workspace-options"],
    queryFn: () => api.get<Options>("/api/v1/workspace/options"),
  });
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/api/v1/settings"),
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.put<Settings>("/api/v1/settings", body),
    onSuccess: () => {
      setSavedAt(new Date().toLocaleTimeString());
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const providerId = form.llm_provider || String(query.data?.app.llm_provider || "moonshot");
  const provider = useMemo(
    () => options.data?.providers.find((item) => item.id === providerId),
    [options.data, providerId],
  );
  const seedanceModels = options.data?.seedance_models || [
    { id: "doubao-seedance-2-0-260128", label: "Seedance 2.0" },
  ];

  useEffect(() => {
    if (!query.data) return;
    const app = query.data.app;
    const seedance = query.data.seedance || {};
    const currentProvider = String(app.llm_provider || "moonshot");
    setForm({
      llm_provider: currentProvider,
      api_key: String(app[`${currentProvider}_api_key`] || ""),
      base_url: String(app[`${currentProvider}_base_url`] || ""),
      model_name: String(app[`${currentProvider}_model_name`] || ""),
      video_sources: parseSources(app.video_sources).join(","),
      pexels_api_keys: keysToText(app.pexels_api_keys),
      pixabay_api_keys: keysToText(app.pixabay_api_keys),
      coverr_api_keys: keysToText(app.coverr_api_keys),
      sonilo_api_key: String(app.sonilo_api_key || ""),
      seedance_api_key: String(seedance.api_key || ""),
      seedance_base_url: String(seedance.base_url || ""),
      seedance_model_id: String(seedance.model_id || ""),
      seedance_resolution: String(seedance.resolution || "720p"),
      mimo_api_key: String(app.mimo_api_key || ""),
      mimo_base_url: String(app.mimo_base_url || ""),
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

  useEffect(() => {
    if (tab !== "llm" || !provider) return;
    const extras: Record<string, string> = {};
    for (const field of provider.extra_fields || []) {
      extras[field.suffix] = form[`extra_${field.suffix}`] || "";
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setProbing(true);
      void api
        .post<{ models: ModelOption[]; error?: string }>("/api/v1/workspace/llm-models", {
          provider: providerId,
          api_key: form.api_key || "",
          base_url: form.base_url || "",
          extra: extras,
        })
        .then((data) => {
          if (cancelled) return;
          setModels(data.models || []);
          setProbeError(data.error || "");
        })
        .catch((error: Error) => {
          if (cancelled) return;
          setModels([]);
          setProbeError(error.message || "模型探测失败");
        })
        .finally(() => {
          if (!cancelled) setProbing(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    form.api_key,
    form.base_url,
    provider,
    providerId,
    tab,
    JSON.stringify(
      (provider?.extra_fields || []).map((field) => form[`extra_${field.suffix}`] || ""),
    ),
  ]);

  const set = (key: string, value: string) => setForm((current) => ({ ...current, [key]: value }));

  if (query.isLoading) {
    return (
      <section className="page">
        <div className="muted">加载配置中…</div>
      </section>
    );
  }

  if (!query.data) {
    return (
      <section className="page">
        <div className="error">{query.error?.message || "无法读取配置"}</div>
      </section>
    );
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">基础配置</h1>
          <p className="muted">按生成链路逐项填写密钥与模型，保存后立即生效。</p>
        </div>
      </div>

      <div className="settings-tabs" role="tablist" aria-label="配置分组">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={`settings-tab ${tab === item.id ? "active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            <span className="settings-tab-label">{item.label}</span>
          </button>
        ))}
      </div>

      <form
        className="settings-panel"
        onSubmit={(event) => {
          event.preventDefault();
          const current = providerId;
          const app: Record<string, unknown> = {
            ...(query.data.app || {}),
            llm_provider: current,
            video_sources: parseSources((form.video_sources || "").split(",")),
            pexels_api_keys: textToKeys(form.pexels_api_keys || ""),
            pixabay_api_keys: textToKeys(form.pixabay_api_keys || ""),
            coverr_api_keys: textToKeys(form.coverr_api_keys || ""),
            sonilo_api_key: form.sonilo_api_key || "",
            mimo_api_key: form.mimo_api_key || "",
            mimo_base_url: form.mimo_base_url || "",
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
              base_url: form.seedance_base_url || "",
              model_id: form.seedance_model_id || "",
              resolution: form.seedance_resolution || "720p",
            },
          });
        }}
      >
        {tab === "seedance" ? (
          <div className="settings-section stack" role="tabpanel">
            <div className="settings-section-head">
              <h2>视频生成模型</h2>
              <p className="muted">
                用于缺镜时自动调用 Seedance 生成分镜视频。未填写 API Key 时，自动剪辑会改用「视频素材」中启用的在线素材源。
              </p>
            </div>
            <Field label="API Key" hint="可留空以回退火山方舟 / ARK 环境变量">
              <input
                type="password"
                autoComplete="off"
                value={form.seedance_api_key || ""}
                onChange={(event) => set("seedance_api_key", event.target.value)}
                placeholder="Seedance / Ark API Key"
              />
            </Field>
            <Field label="模型">
              <select
                value={form.seedance_model_id || seedanceModels[0]?.id || ""}
                onChange={(event) => set("seedance_model_id", event.target.value)}
              >
                {seedanceModels.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}（{item.id}）
                  </option>
                ))}
                {form.seedance_model_id &&
                !seedanceModels.some((item) => item.id === form.seedance_model_id) ? (
                  <option value={form.seedance_model_id}>{form.seedance_model_id}</option>
                ) : null}
              </select>
            </Field>
            <div className="stack two">
              <Field label="接口地址">
                <input
                  value={form.seedance_base_url || ""}
                  onChange={(event) => set("seedance_base_url", event.target.value)}
                  placeholder="https://ark.cn-beijing.volces.com/api/v3"
                />
              </Field>
              <Field label="清晰度">
                <select
                  value={form.seedance_resolution || "720p"}
                  onChange={(event) => set("seedance_resolution", event.target.value)}
                >
                  <option value="480p">480p</option>
                  <option value="720p">720p</option>
                  <option value="1080p">1080p</option>
                </select>
              </Field>
            </div>
          </div>
        ) : null}

        {tab === "materials" ? (
          <div className="settings-section stack" role="tabpanel">
            <div className="settings-section-head">
              <h2>视频素材与素材库</h2>
              <p className="muted">
                勾选自动剪辑可用的素材源。未配置视频生成 API Key 时，系统会按这些来源检索在线素材并完成剪辑。
              </p>
            </div>
            <div className="settings-field">
              <span className="settings-label">启用素材源</span>
              <div className="settings-checks">
                {STOCK_SOURCES.map((item) => {
                  const selected = parseSources((form.video_sources || "").split(","));
                  return (
                    <label key={item.id} className="settings-check">
                      <input
                        type="checkbox"
                        checked={selected.includes(item.id)}
                        onChange={() => set("video_sources", toggleSource(selected, item.id).join(","))}
                      />
                      <span>{item.label}</span>
                    </label>
                  );
                })}
              </div>
              <span className="settings-hint">至少保留一项；Pexels / Pixabay / Coverr 需要对应密钥</span>
            </div>
            <Field label="Pexels API Keys" hint="每行一个密钥，申请：https://www.pexels.com/api/">
              <textarea
                value={form.pexels_api_keys || ""}
                onChange={(event) => set("pexels_api_keys", event.target.value)}
                placeholder={"key-1\nkey-2"}
              />
            </Field>
            <Field label="Pixabay API Keys" hint="每行一个密钥，申请：https://pixabay.com/api/docs/">
              <textarea
                value={form.pixabay_api_keys || ""}
                onChange={(event) => set("pixabay_api_keys", event.target.value)}
                placeholder={"key-1\nkey-2"}
              />
            </Field>
            <Field label="Coverr API Keys" hint="每行一个密钥，申请：https://coverr.co/developers">
              <textarea
                value={form.coverr_api_keys || ""}
                onChange={(event) => set("coverr_api_keys", event.target.value)}
                placeholder={"key-1\nkey-2"}
              />
            </Field>
            <Field label="Sonilo API Key" hint="可选，用于视频匹配配乐">
              <input
                type="password"
                autoComplete="off"
                value={form.sonilo_api_key || ""}
                onChange={(event) => set("sonilo_api_key", event.target.value)}
                placeholder="Sonilo API Key"
              />
            </Field>
          </div>
        ) : null}

        {tab === "llm" ? (
          <div className="settings-section stack" role="tabpanel">
            <div className="settings-section-head">
              <h2>文案生成 LLM</h2>
              <p className="muted">用于脚本、导演规划与分镜提示词生成。填写密钥后会自动探测可用模型。</p>
            </div>
            <Field label="提供商" name="llm_provider">
              <select value={providerId} onChange={(event) => set("llm_provider", event.target.value)}>
                {(options.data?.providers || []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </Field>
            {provider?.show_api_key ? (
              <Field
                label="API Key"
                name={`${providerId}_api_key`}
                hint={provider.api_key_url ? `申请地址：${provider.api_key_url}` : undefined}
              >
                <input
                  type="password"
                  autoComplete="off"
                  value={form.api_key || ""}
                  onChange={(event) => set("api_key", event.target.value)}
                  placeholder="API Key"
                />
              </Field>
            ) : null}
            {provider?.show_base_url ? (
              <Field label="Base URL" name={`${providerId}_base_url`}>
                <input
                  value={form.base_url || ""}
                  onChange={(event) => set("base_url", event.target.value)}
                  placeholder={provider.default_base_url || "https://api.example.com/v1"}
                />
              </Field>
            ) : null}
            {(provider?.extra_fields || []).map((field) => (
              <Field key={field.suffix} label={field.label} name={`${providerId}_${field.suffix}`}>
                <input
                  type={field.secret ? "password" : "text"}
                  autoComplete="off"
                  value={form[`extra_${field.suffix}`] || ""}
                  onChange={(event) => set(`extra_${field.suffix}`, event.target.value)}
                  placeholder={field.label}
                />
              </Field>
            ))}
            <Field
              label="模型"
              name={`${providerId}_model_name`}
              hint={
                probing
                  ? "正在探测可用模型…"
                  : probeError
                    ? probeError
                    : models.length
                      ? `已探测到 ${models.length} 个模型`
                      : "填写密钥后自动探测"
              }
            >
              <select
                value={form.model_name || ""}
                onChange={(event) => set("model_name", event.target.value)}
              >
                <option value="">{provider?.default_model ? `默认（${provider.default_model}）` : "请选择模型"}</option>
                {form.model_name && !models.some((item) => item.id === form.model_name) ? (
                  <option value={form.model_name}>{form.model_name}</option>
                ) : null}
                {models.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        ) : null}

        {tab === "tts" ? (
          <div className="settings-section stack" role="tabpanel">
            <div className="settings-section-head">
              <h2>TTS 语音合成</h2>
              <p className="muted">当前仅使用小米 MiMo 合成旁白，填写 API Key 后即可在自动剪辑中选择音色。</p>
            </div>
            <Field label="API Key">
              <input
                type="password"
                autoComplete="off"
                value={form.mimo_api_key || ""}
                onChange={(event) => set("mimo_api_key", event.target.value)}
                placeholder="MiMo API Key"
              />
            </Field>
            <Field label="Base URL" hint="可选，留空使用默认接口">
              <input
                value={form.mimo_base_url || ""}
                onChange={(event) => set("mimo_base_url", event.target.value)}
                placeholder="https://api.xiaomimimo.com/v1"
              />
            </Field>
          </div>
        ) : null}

        <div className="settings-footer">
          <button className="primary" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "保存中…" : "保存当前配置"}
          </button>
          {savedAt && !mutation.error ? (
            <span className="muted">已保存 {savedAt}</span>
          ) : null}
          {mutation.error ? <span className="error">{mutation.error.message}</span> : null}
        </div>
      </form>
    </section>
  );
}
