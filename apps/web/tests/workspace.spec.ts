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
  await expect(
    page.getByText("demo-paper.pdf", { exact: false }),
  ).toBeVisible({ timeout: 45000 });
  await page
    .getByLabel("你想完成什么研究？")
    .fill("解释提供论文的方法原理、实验设计和局限，给出可检验的后续研究方向。");
  await page.getByRole("button", { name: "开始研究 →" }).click();
  await expect(
    page.getByText("SYNTHETIC · Research package", { exact: true }),
  ).toBeVisible({ timeout: 45000 });
  await page.getByRole("button", { name: "查看证据 ↗" }).first().click();
  await expect(page.locator("canvas")).toBeVisible();
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
