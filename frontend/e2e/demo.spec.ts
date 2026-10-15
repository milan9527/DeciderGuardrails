import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

const scenarios = Array.from({ length: 8 }, (_, index) => ({
  id: `scenario-${index}`,
  title:
    index === 0
      ? "客户信息保护"
      : index === 4
        ? "客服回答核验"
        : `场景 ${index + 1}`,
  icon: index === 4 ? "document" : "shield",
  category: index === 4 ? "grounding" : "pii",
  description: "区分敏感信息与脱敏内容。",
  goal: "固定规则的边界案例。",
  cases: ["zh", "en"].flatMap((language) =>
    ["block", "allow"].map((expected) => ({
      id: `${index}-${language}-${expected}`,
      language,
      expected,
      label: `应${expected === "block" ? "拦截" : "放行"} · ${language === "zh" ? "中文" : "English"}`,
      content:
        expected === "block"
          ? "客户邮箱是 alice@example.com。"
          : "客户邮箱为[已脱敏]。",
      source: index === 4 ? "OUTPUT" : "INPUT",
      reference: index === 4 ? "商品可在30天内退货。" : null,
    })),
  ),
}));

test.beforeEach(async ({ page }) => {
  let loggedIn = false;
  await page.route("**/api/scenarios", (route) => {
    expect(loggedIn).toBe(true);
    return route.fulfill({ json: { scenarios } });
  });
  await page.route("**/api/session", (route) =>
    route.fulfill(
      loggedIn
        ? { json: { authenticated: true, username: "admin" } }
        : { status: 401, json: { detail: "请先登录。" } },
    ),
  );
  await page.route("**/api/login", (route) => {
    expect(route.request().headers()["x-requested-with"]).toBe(
      "DeciderGuardrails",
    );
    const login = route.request().postDataJSON();
    loggedIn =
      login.username === "admin" && login.password === "browser-test-password";
    return route.fulfill(
      loggedIn
        ? { json: { authenticated: true, username: "admin" } }
        : { status: 401, json: { detail: "用户名或密码错误。" } },
    );
  });
  await page.route("**/api/logout", (route) => {
    loggedIn = false;
    return route.fulfill({ json: { authenticated: false } });
  });
  await page.route("**/api/evaluate", (route) => {
    expect(loggedIn).toBe(true);
    const request = route.request().postDataJSON();
    expect(route.request().headers()).not.toHaveProperty("authorization");
    expect(route.request().headers()["x-requested-with"]).toBe(
      "DeciderGuardrails",
    );
    expect(request).not.toHaveProperty("expected");
    const score = request.content.includes("已脱敏") ? 0.1 : 0.8;
    return route.fulfill({
      json: {
        request_id: "browser-test-" + Date.now(),
        status: "ok",
        decision: score >= request.threshold ? "block" : "allow",
        mode: request.mode,
        score,
        threshold: request.threshold,
        latency_ms: 125,
        server_ms: 110,
        model: "test-only-fixture",
        usage: { input_tokens: 120 },
        policy_version: "test",
        active_checks: ["pii"],
        score_note: "测试数据。",
        checks: [
          {
            id: "pii",
            name: "敏感信息",
            score,
            triggered: score >= request.threshold,
            policy: "Specific sensitive values are blocked.",
          },
        ],
      },
    });
  });
  await page.goto("/");
});

async function unlock(page: Page) {
  await expect(page.getByRole("heading", { name: "登录演示" })).toBeVisible();
  await page.getByLabel("用户名", { exact: true }).fill("admin");
  await page.getByLabel("密码", { exact: true }).fill("browser-test-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "客户信息保护", exact: true }),
  ).toBeVisible();
}

test("runs both model modes and distinguishes allow from block", async ({
  page,
}) => {
  await unlock(page);
  await page.getByLabel("检查方式", { exact: true }).selectOption("compare");
  await page.getByRole("button", { name: "运行对比" }).click();
  await expect(page.getByText("建议拦截")).toBeVisible();
  await expect(page.getByText("与该示例的预期结果一致")).toBeVisible();
  await page
    .getByRole("button", { name: "应放行 · 中文", exact: false })
    .click();
  await page.getByRole("button", { name: "运行对比" }).click();
  await expect(page.getByText("建议放行")).toBeVisible();
});

test("custom content is sent without the original expected label", async ({
  page,
}) => {
  await unlock(page);
  await page
    .getByLabel("待检查内容")
    .fill('<script>alert("fixture")</script> [已脱敏]');
  await expect(page.getByText("用于对照，不会发送给模型")).toBeHidden();
  await page.getByRole("button", { name: "运行检查" }).click();
  await expect(page.getByText("建议放行")).toBeVisible();
  await expect(page.getByText("与该示例的预期结果一致")).toBeHidden();
});

test("upstream errors are shown separately from a model block", async ({
  page,
}) => {
  await unlock(page);
  await page.route("**/api/evaluate", (route) =>
    route.fulfill({
      status: 502,
      json: {
        status: "error",
        error: "模型服务暂不可用。",
        enforcement_action: "block",
      },
    }),
  );
  await page.getByRole("button", { name: "运行检查" }).click();
  await expect(page.getByText("模型服务暂不可用。")).toBeVisible();
  await expect(page.getByText("建议拦截")).toBeHidden();
});

test("runs 32 scenario cases and reports measured accuracy", async ({
  page,
}) => {
  await unlock(page);
  await page.getByRole("button", { name: "场景回归" }).click();
  await page.getByRole("button", { name: "运行全部场景" }).click();
  await expect(page.getByText("已完成 32 / 32")).toBeVisible();
  await expect(page.getByText("100.0%")).toBeVisible();
  await expect(page.getByRole("button", { name: "导出结果" })).toBeEnabled();
  await page.getByLabel("回归检查方式").selectOption("compact");
  const pendingDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出结果" }).click();
  const download = await pendingDownload;
  const text = await readFile((await download.path())!, "utf8");
  const report = JSON.parse(text);
  expect(report.config.mode).toBe("detailed");
  expect(report.config.threshold).toBe(0.5);
  expect(text).not.toContain("browser-test-password");
});

test("login survives refresh, stores no password, and logout requires a new login", async ({
  page,
}) => {
  await unlock(page);
  await expect(
    page.getByRole("button", { name: "已登录 · admin" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ),
  ).not.toContain("browser-test-password");
  await page.reload();
  await expect(
    page.getByRole("button", { name: "已登录 · admin" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "已登录 · admin" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(
    page.getByRole("button", { name: "登录", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "登录演示" })).toBeVisible();
  await expect(page.getByRole("button", { name: "运行检查" })).toBeHidden();
  await expect(page.locator(".app-shell")).toHaveCount(0);
});

test("wrong password displays a login error and clears the password field", async ({
  page,
}) => {
  await page.getByLabel("用户名", { exact: true }).fill("admin");
  await page.getByLabel("密码", { exact: true }).fill("wrong");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("用户名或密码错误。")).toBeVisible();
  await expect(page.getByLabel("密码", { exact: true })).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "已登录 · admin" }),
  ).toBeHidden();
});

test("output checks expose context fields and mobile layout does not overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await unlock(page);
  await page.getByRole("button", { name: "客服回答核验", exact: true }).click();
  await expect(
    page.getByPlaceholder("粘贴可作为依据的政策、知识库片段…"),
  ).toHaveValue("商品可在30天内退货。");
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});

test("home and direct routes show only login without fetching workspace data", async ({
  page,
}) => {
  const catalogRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/scenarios"))
      catalogRequests.push(request.url());
  });
  await page.goto("/workspace");
  await expect(page.getByRole("heading", { name: "登录演示" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.locator(".login-footer").click();
  await expect(page.getByLabel("用户名")).toBeVisible();
  await expect(page.locator(".app-shell")).toHaveCount(0);
  await expect(page.getByRole("navigation")).toHaveCount(0);
  expect(catalogRequests).toHaveLength(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});

test("slow session validation never reveals the workspace", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/session", async (route) => {
    await pending;
    await route.fulfill({ status: 401, json: { detail: "请先登录。" } });
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  try {
    await expect(
      page.getByRole("heading", { name: "正在确认登录状态" }),
    ).toBeVisible();
    await expect(page.locator(".app-shell")).toHaveCount(0);
    await expect(page.getByLabel("待检查内容")).toHaveCount(0);
  } finally {
    release();
  }
  await expect(page.getByRole("heading", { name: "登录演示" })).toBeVisible();
});

test("an expired session returns to login and clears the previous workspace", async ({
  page,
}) => {
  await unlock(page);
  await page.getByLabel("待检查内容").fill("private-workspace-draft");
  await page.route("**/api/evaluate", (route) =>
    route.fulfill({ status: 401, json: { detail: "会话已过期。" } }),
  );
  await page.getByRole("button", { name: "运行检查" }).click();
  await expect(page.getByRole("heading", { name: "登录演示" })).toBeVisible();
  await expect(page.locator(".app-shell")).toHaveCount(0);
  await unlock(page);
  await expect(page.getByLabel("待检查内容")).not.toHaveValue(
    "private-workspace-draft",
  );
});
