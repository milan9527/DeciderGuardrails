export type Mode = "compact" | "detailed";
export type Decision = "allow" | "block";
export type Sample = {
  id: string;
  label: string;
  content: string;
  expected: Decision;
  source: "INPUT" | "OUTPUT";
  reference?: string | null;
  query?: string | null;
  language: string;
  split: string;
};
export type Scenario = {
  id: string;
  title: string;
  description: string;
  goal: string;
  category: string;
  icon: string;
  cases: Sample[];
};
export type Evaluation = {
  request_id: string;
  status: "ok";
  decision: Decision;
  mode: Mode;
  score: number;
  threshold: number;
  latency_ms: number;
  server_ms: number | null;
  model: string;
  usage: { input_tokens?: number; output_tokens?: number };
  policy_version: string;
  active_checks: string[];
  score_note: string;
  checks: {
    id: string;
    name: string;
    score: number;
    triggered: boolean;
    policy: string;
  }[];
};
export type EvaluationInput = {
  content: string;
  source: "INPUT" | "OUTPUT";
  reference?: string;
  query?: string;
  mode: Mode;
  threshold: number;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 35000);
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(`/api/${path}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      headers: {
        ...(body !== undefined
          ? {
              "Content-Type": "application/json",
              "X-Requested-With": "DeciderGuardrails",
            }
          : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new ApiError("服务未返回有效结果，请稍后重试。", response.status);
    }
    if (!response.ok) {
      const message =
        typeof data.detail === "string" ? data.detail : data.error;
      throw new ApiError(
        message || "请求未完成，请检查输入或稍后重试。",
        response.status,
      );
    }
    return data as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw new ApiError("请求已取消或超时；未获得模型判断。", 408);
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
