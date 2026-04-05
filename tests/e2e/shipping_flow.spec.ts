import { expect, test } from "@playwright/test";

test.describe("Shipping flow", () => {
  test("loads shipping queues and label purchase surface", async ({ page }) => {
    let appReady = false;
    for (let i = 0; i < 20; i += 1) {
      try {
        await page.goto("/", { waitUntil: "domcontentloaded", timeout: 4000 });
        appReady = true;
        break;
      } catch {
        await page.waitForTimeout(1000);
      }
    }
    expect(appReady).toBeTruthy();

    const signInButton = page.getByRole("button", { name: /Sign In/i }).first();
    const authGateAlert = page.getByText(/^Sign in required\.$/i).first();
    const usernameInput = page.getByLabel("Username", { exact: true }).first();
    const passwordInput = page.getByLabel("Password", { exact: true }).first();
    const username = process.env.E2E_USERNAME || "e2e";
    const password = process.env.E2E_PASSWORD || "";
    const withCurrentAuth = (path: string): string => {
      const token = new URL(page.url()).searchParams.get("auth");
      return token ? `${path}?auth=${encodeURIComponent(token)}` : path;
    };
    test.skip(!username || !password, "Auth is enabled locally; set E2E_USERNAME/E2E_PASSWORD to run this test.");

    try {
      await usernameInput.waitFor({ state: "visible", timeout: 12000 });
      await passwordInput.waitFor({ state: "visible", timeout: 12000 });
      await signInButton.waitFor({ state: "visible", timeout: 12000 });
      await usernameInput.fill(username);
      await passwordInput.fill(password);
      await page.getByLabel("Remember me on this browser").first().check();
      await signInButton.click();
      await expect
        .poll(async () => {
          const visible = await signInButton.isVisible().catch(() => false);
          const required = await authGateAlert.isVisible().catch(() => false);
          return !visible && !required;
        }, { timeout: 15000 })
        .toBeTruthy();
    } catch {
      // Password auth form may not be shown in auth-disabled environments.
    }

    await page.goto(withCurrentAuth("/Shipping"));
    await expect(page.getByRole("heading", { name: /^Shipping$/i }).first()).toBeVisible({ timeout: 15000 });

    const needsLabel = page.getByRole("tab", { name: /Needs Label/i }).first();
    const inTransit = page.getByRole("tab", { name: /In Transit/i }).first();
    const exceptions = page.getByRole("tab", { name: /Exceptions/i }).first();
    await expect
      .poll(async () => {
        const hasQueues =
          (await needsLabel.isVisible().catch(() => false)) &&
          (await inTransit.isVisible().catch(() => false)) &&
          (await exceptions.isVisible().catch(() => false));
        const emptyState = await page.getByText(/No sales records yet|No sales match/i).first().isVisible().catch(() => false);
        return hasQueues || emptyState;
      }, { timeout: 15000 })
      .toBeTruthy();

    const labelQueueHeading = page.getByRole("heading", { name: /Label Purchase Queue/i }).first();
    const shippingEmpty = page.getByText(/No sales records yet|No sales match/i).first();
    await expect
      .poll(async () => {
        const queueVisible = await labelQueueHeading.isVisible().catch(() => false);
        const emptyVisible = await shippingEmpty.isVisible().catch(() => false);
        return queueVisible || emptyVisible;
      }, { timeout: 15000 })
      .toBeTruthy();

    const bulkUpdate = page.getByRole("button", { name: /Apply Bulk Update/i }).first();
    if (await bulkUpdate.isVisible().catch(() => false)) {
      await bulkUpdate.click();
      await expect(page.getByText(/Select at least one sale\./i).first()).toBeVisible({ timeout: 10000 });
    }
  });
});
