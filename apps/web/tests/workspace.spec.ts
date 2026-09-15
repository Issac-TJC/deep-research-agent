import { test, expect } from "@playwright/test";
import fs from "node:fs";
const keyFile = "../../.local/test-tenants.jsonl";
const key = JSON.parse(
  fs.readFileSync(keyFile, "utf8").trim().split("\n")[0],
).api_key;
test("create research, inspect evidence, download report, and retain session", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByLabel("API Key").fill(key);
  await page.getByRole("button", { name: "连接工作区", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "从一个值得研究的问题开始" }),
  ).toBeVisible();
  await page.screenshot({
    path: "../../artifacts/ui/new-research.png",
    fullPage: true,
  });
  await page
    .getByLabel("你想完成什么研究？")
    .fill(
      "为中文技术知识库比较关键词、向量、混合检索与重排，重点关注错误码和成本。",
    );
  await page.getByRole("button", { name: "开始研究 →" }).click();
  await expect(
    page.getByText("SYNTHETIC · Research package", { exact: true }),
  ).toBeVisible({ timeout: 45000 });
  await page.getByRole("button", { name: "查看证据 ↗" }).first().click();
  await expect(page.getByText("原文检查器", { exact: true })).toBeVisible();
  await expect(page.locator("mark")).toBeVisible();
  await page.screenshot({
    path: "../../artifacts/ui/research-evidence.png",
    fullPage: true,
  });
  const download = page.waitForEvent("download");
  await page.getByText("导出 Markdown ↓").click();
  expect((await download).suggestedFilename()).toContain("research");
  await page.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "持久化事件" })).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "从一个值得研究的问题开始" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
test("PDF upload and paper understanding are accessible on mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByLabel("API Key").fill(key);
  await page.getByRole("button", { name: "连接工作区", exact: true }).click();
  await page
    .locator("input[type=file]")
    .setInputFiles("../../artifacts/demo-paper.pdf");
  await expect(page.getByText("demo-paper.pdf", { exact: false })).toBeVisible({
    timeout: 45000,
  });
  const secondPdf = Buffer.concat([
    fs.readFileSync("../../artifacts/demo-paper.pdf"),
    Buffer.from("\n% second PDF fixture\n"),
  ]);
  await page.locator("input[type=file]").setInputFiles({
    name: "demo-paper-second.pdf",
    mimeType: "application/pdf",
    buffer: secondPdf,
  });
  await expect(page.getByText("demo-paper-second.pdf", { exact: false })).toBeVisible({
    timeout: 45000,
  });
  await page
    .getByLabel("你想完成什么研究？")
    .fill("解释提供论文的方法原理、实验设计和局限，给出可检验的后续研究方向。");
  await page.getByRole("button", { name: "开始研究 →" }).click();
  await expect(
    page.getByText("SYNTHETIC · Research package", { exact: true }),
  ).toBeVisible({ timeout: 45000 });
  const evidenceButtons = page.getByRole("button", { name: "查看证据 ↗" });
  expect(await evidenceButtons.count()).toBeGreaterThanOrEqual(4);
  const openedSources = new Set<string>();
  for (let index = 0; index < (await evidenceButtons.count()); index += 1) {
    await evidenceButtons.nth(index).click();
    await expect(page.locator(".inspector h3")).toBeVisible();
    openedSources.add((await page.locator(".inspector h3").textContent()) || "");
    await expect(page.locator("canvas")).toBeVisible();
    if (openedSources.size === 2) break;
    await page.getByRole("button", { name: "关闭 ×" }).click();
  }
  expect([...openedSources].sort()).toEqual([
    "demo-paper-second.pdf",
    "demo-paper.pdf",
  ]);
  await expect(page.getByText("PDF 预览失败", { exact: false })).toHaveCount(0);
  await page.screenshot({
    path: "../../artifacts/ui/paper-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("project memory and weekly digest form one workspace loop", async ({
  page,
}) => {
  const name = `Project workspace ${Date.now()}`;
  await page.goto("/");
  await page.getByLabel("API Key").fill(key);
  await page.getByRole("button", { name: "连接工作区", exact: true }).click();
  await page.getByRole("button", { name: "＋ 新建项目" }).click();
  await page.getByLabel("项目名称").fill(name);
  await page.getByLabel("项目目标").fill("持续跟踪高质量的 3DGS 压缩研究");
  await page.getByLabel("项目标签").fill("3DGS, compression");
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(
    page.locator("header").getByText(name, { exact: false }),
  ).toBeVisible();

  await page.getByRole("tab", { name: /^记忆/ }).click();
  await expect(
    page.getByRole("heading", { name: "受治理的混合记忆" }),
  ).toBeVisible();
  await expect(
    page.getByText("持续跟踪高质量的 3DGS 压缩研究", { exact: true }),
  ).toBeVisible();

  await page.getByRole("tab", { name: /^周报/ }).click();
  await page.getByLabel("订阅主题").fill("3DGS compression");
  await page.getByLabel("查询词").fill("Gaussian Splatting, compression");
  await page.getByRole("button", { name: "创建每周订阅" }).click();
  await expect(
    page.getByText("3DGS compression", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "试运行查询" }).click();
  await expect(page.locator(".error")).toContainText("试运行已从");
  await page.locator(".error").getByRole("button", { name: "关闭" }).click();
  await page.getByRole("button", { name: "立即生成周报" }).click();
  await expect(page.getByRole("heading", { name: "研究进行时" })).toBeVisible();
  await expect(
    page.getByText("SYNTHETIC · Research package", { exact: true }),
  ).toBeVisible({
    timeout: 45000,
  });
  await page.getByRole("button", { name: new RegExp(`^${name}`) }).click();
  await page.getByRole("tab", { name: /^周报/ }).click();
  await expect(page.getByText("needs_review", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "查看周报报告" }),
  ).toBeVisible();
  await page.screenshot({
    path: "../../artifacts/ui/project-weekly.png",
    fullPage: true,
  });
});

test("core workspace controls have keyboard focus and accessible names", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("API Key").fill(key);
  await page.keyboard.press("Tab");
  const focused = page.locator(":focus");
  await expect(focused).toHaveAccessibleName("连接工作区");
  await expect(focused).toBeVisible();
  const outline = await focused.evaluate((element) => getComputedStyle(element).outlineStyle);
  expect(outline).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", { name: "从一个值得研究的问题开始" }),
  ).toBeVisible();
  const violations = await page.evaluate(() => {
    const issues: string[] = [];
    document.querySelectorAll("button").forEach((button, index) => {
      if (!(button.textContent || button.getAttribute("aria-label") || "").trim())
        issues.push(`button-${index}-missing-name`);
    });
    document.querySelectorAll("input,textarea,select").forEach((control, index) => {
      const id = control.getAttribute("id");
      const labelled =
        control.getAttribute("aria-label") ||
        control.closest("label") ||
        (id && document.querySelector(`label[for="${id}"]`));
      if (!labelled) issues.push(`control-${index}-missing-label`);
    });
    return issues;
  });
  expect(violations).toEqual([]);
});

test("confirming a candidate memory refreshes without a proxy 404", async ({ page }) => {
  const name = `Memory confirmation ${Date.now()}`;
  await page.goto("/");
  await page.getByLabel("API Key").fill(key);
  await page.getByRole("button", { name: "连接工作区", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "从一个值得研究的问题开始" }),
  ).toBeVisible();
  await expect(page.getByText("真实研究模式", { exact: true })).toBeVisible();
  const project = await page.evaluate(
    async ({ projectName, idempotencyKey }) => {
      const response = await fetch("/api/backend/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({
          name: projectName,
          objective: "验证候选记忆确认后的完整刷新",
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    },
    { projectName: name, idempotencyKey: crypto.randomUUID() },
  );
  try {
    const memoryCreated = await page.evaluate(
      async ({ projectId, idempotencyKey }) => {
        const response = await fetch(
          `/api/backend/projects/${projectId}/memories`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": idempotencyKey,
            },
            body: JSON.stringify({
              type: "constraint",
              content: "候选记忆确认代理回归",
              status: "candidate",
            }),
          },
        );
        return response.ok;
      },
      { projectId: project.id, idempotencyKey: crypto.randomUUID() },
    );
    expect(memoryCreated).toBe(true);
    await page.reload();
    await page.getByRole("button", { name: new RegExp(`^${name}`) }).click();
    await page.getByRole("tab", { name: /^记忆/ }).click();
    await page.getByRole("tab", { name: /^待确认/ }).click();
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await page.getByRole("tab", { name: /^项目知识/ }).click();
    await expect(
      page.getByText("constraint · confirmed", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".error")).toHaveCount(0);
  } finally {
    await page.evaluate(async (projectId) => {
      await fetch(`/api/backend/projects/${projectId}`, { method: "DELETE" });
    }, project.id);
  }
});
