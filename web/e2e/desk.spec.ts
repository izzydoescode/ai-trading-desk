import { expect, test, type Page } from "@playwright/test";

const JSON_HEADERS = { "content-type": "application/json" };

/** Every test starts from a stopped desk with the kill switch released. */
test.beforeEach(async ({ page, request }) => {
  await request.post("/api/replay/stop", { headers: JSON_HEADERS, data: {} });
  await request.post("/api/kill-switch", { headers: JSON_HEADERS, data: { engaged: false } });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "AI TRADING DESK" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0); // connected: no CONNECTING badge
});

const region = (page: Page, name: string) => page.getByRole("region", { name });
const tile = (page: Page, panel: string, label: string) =>
  region(page, panel).locator(".tile", { hasText: label }).locator("dd");
const startButton = (page: Page) => page.getByRole("button", { name: /START REPLAY|REPLAY AGAIN/ });
const header = (page: Page) => page.getByRole("banner");

test("a replay runs from the first bar to a closed paper trade", async ({ page }) => {
  await startButton(page).click();
  await expect(header(page).getByText("RUNNING", { exact: true })).toBeVisible();
  await expect(header(page).getByText("FINISHED", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(header(page).getByText("BAR 80/80")).toBeVisible();

  // Six agents reported, the desk traded once on paper, and the journal says why it exited.
  for (const agent of ["HAWKEYE", "NEO", "SNOWDEN", "FAMA", "ODIN", "PICARD"]) {
    await expect(region(page, "Agents").getByRole("heading", { name: agent })).toBeVisible();
  }
  await expect(tile(page, "Balance", "TRADES")).toHaveText("1");
  await expect(region(page, "Balance").locator(".figure")).not.toHaveText("$100,000.00");
  await expect(region(page, "Journal").getByText("EXIT", { exact: true })).toBeVisible();
  await expect(region(page, "Risk checks").getByText(/^(APPROVE|REVISE|REJECT) \d\/9$/)).toBeVisible();
  await expect(page.getByRole("img", { name: /AAPL daily candles/ })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Decision pipeline" }).getByText(/^PICARD /)).toBeVisible();
  await expect(page.getByText("Paper trading only.")).toBeVisible();

  // The review page has the closed trade, and its saved decision opens from the journal.
  const review = region(page, "Trade review");
  await expect(review.getByText(/^1W \/ 0L · \+\$/)).toBeVisible();
  await expect(review.getByRole("table", { name: "BY STRATEGY" }).getByRole("row", { name: /^TREND 1 100%/ })).toBeVisible();
  await review.getByRole("button", { name: "Why trade 1 was taken" }).click();
  const decision = review.getByRole("region", { name: "Saved decision" });
  await expect(decision.getByText("PLANNED ENTRY")).toBeVisible();
  await expect(decision.getByText(/^ODIN APPROVE:/)).toBeVisible();
});

test("pause holds the bar and resume carries on to the end", async ({ page }) => {
  await page.getByRole("combobox", { name: /Replay speed/ }).selectOption("4");
  await startButton(page).click();
  await page.getByRole("button", { name: "PAUSE" }).click();
  await expect(header(page).getByText("PAUSED", { exact: true })).toBeVisible();

  const barPosition = header(page).getByText(/^BAR \d+\/80$/);
  const heldAt = await barPosition.textContent();
  await page.waitForTimeout(1200); // at 4 bars a second an unpaused run would move ~5 bars
  await expect(barPosition).toHaveText(heldAt!);

  await page.getByRole("combobox", { name: /Replay speed/ }).selectOption("32");
  await page.getByRole("button", { name: "RESUME" }).click();
  await expect(header(page).getByText("FINISHED", { exact: true })).toBeVisible({ timeout: 30_000 });
});

test("the kill switch halts the desk: no ideas, no trades, balance untouched", async ({ page }) => {
  await page.getByRole("button", { name: "KILL SWITCH" }).click();
  await expect(page.getByRole("alert")).toContainText("KILL SWITCH ENGAGED");
  await expect(region(page, "Agents").getByText("HALTED", { exact: true })).toHaveCount(6);

  await startButton(page).click();
  await expect(header(page).getByText("RUNNING", { exact: true })).toBeVisible();
  await expect(header(page).getByText("FINISHED", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(header(page).getByText("BAR 80/80")).toBeVisible();
  await expect(tile(page, "Balance", "TRADES")).toHaveText("0");
  await expect(region(page, "Balance").locator(".figure")).toHaveText("$100,000.00");
  await expect(region(page, "Decision").getByText("No decision yet.")).toBeVisible();
  await expect(region(page, "Journal").getByText("HALT", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /RELEASE KILL SWITCH/ }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("the command line steers the desk and explains what it cannot do", async ({ page }) => {
  const command = page.getByLabel("DESK>");
  await command.fill("speed 32");
  await command.press("Enter");
  await expect(page.getByRole("combobox", { name: /Replay speed/ })).toHaveValue("32");

  await command.fill("tsla");
  await command.press("Enter");
  await expect(page.getByRole("alert")).toContainText("TSLA is not in this replay");

  await command.fill("start");
  await command.press("Enter");
  await expect(header(page).getByText("RUNNING", { exact: true })).toBeVisible();
  await expect(header(page).getByText("FINISHED", { exact: true })).toBeVisible({ timeout: 30_000 });
});

test("going live is refused in plain words and nothing changes", async ({ page }) => {
  await page.getByRole("button", { name: "GO LIVE" }).click();
  await expect(page.getByRole("alert")).toContainText("Live polling needs a live data source");
  await expect(header(page).getByText("REPLAY", { exact: true })).toBeVisible();
  await expect(header(page).getByText("PAPER", { exact: true })).toBeVisible();
});

test("a second browser tab joins mid-run and sees the same desk", async ({ page, context }) => {
  await page.getByRole("combobox", { name: /Replay speed/ }).selectOption("8");
  await startButton(page).click();
  await page.getByRole("button", { name: "PAUSE" }).click();
  await expect(header(page).getByText("PAUSED", { exact: true })).toBeVisible();

  const secondTab = await context.newPage();
  await secondTab.goto("/");
  await expect(header(secondTab).getByText("PAUSED", { exact: true })).toBeVisible();
  const barPosition = await header(page).getByText(/^BAR \d+\/80$/).textContent();
  await expect(header(secondTab).getByText(barPosition!)).toBeVisible();
});
