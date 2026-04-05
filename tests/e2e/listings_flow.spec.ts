import { expect, test } from "@playwright/test";

test.describe("Listings flow", () => {
  test("creates draft listing and completes review action", async ({ page }) => {
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
    const withCurrentAuth = (path: string): string => {
      const token = new URL(page.url()).searchParams.get("auth");
      return token ? `${path}?auth=${encodeURIComponent(token)}` : path;
    };
    const password = process.env.E2E_PASSWORD || "";
    test.skip(!username || !password, "Auth is enabled locally; set E2E_USERNAME/E2E_PASSWORD to run this test.");
    try {
      await usernameInput.waitFor({ state: "visible", timeout: 12000 });
      await passwordInput.waitFor({ state: "visible", timeout: 12000 });
      await signInButton.waitFor({ state: "visible", timeout: 12000 });
      await usernameInput.fill(username);
      await passwordInput.fill(password);
      await expect(passwordInput).toHaveValue(password);
      await page.getByLabel("Remember me on this browser").first().check();
      await signInButton.click();
      await page.waitForTimeout(300);
      const invalidLogin = page.getByText(/Invalid username\/password/i).first();
      if (await invalidLogin.isVisible().catch(() => false)) {
        test.skip(true, "Local auth credentials rejected; rerun seed and verify E2E_USERNAME/E2E_PASSWORD.");
      }
      await expect.poll(async () => {
        const visible = await signInButton.isVisible().catch(() => false);
        const stillRequired = await authGateAlert.isVisible().catch(() => false);
        return !visible && !stillRequired;
      }, { timeout: 15000 }).toBeTruthy();
    } catch {
      // Auth form not present (password auth disabled) or not required in this environment.
    }

    const uniqueSku = `E2E-LIST-${Date.now()}`;
    const listingTitle = `E2E Listing ${uniqueSku}`;

    await page.goto(withCurrentAuth("/Listings"));
    await expect(page.getByText(/Marketplace Listings/i).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Create-flow eBay readiness preview/i).first()).toBeVisible({ timeout: 15000 });

    await page.getByLabel("Listing Title", { exact: true }).first().fill(listingTitle);
    await page.getByLabel("Listing Price", { exact: true }).first().fill("39");
    await page.getByRole("button", { name: /Create Listing/i }).first().click();
    await expect(page.getByText(/Listing created\./i).first()).toBeVisible({ timeout: 20000 });

    await page.getByLabel("Search Title / External ID", { exact: true }).first().fill(listingTitle);
    await page.waitForTimeout(600);

    await page.getByRole("button", { name: /Approve Listing Review/i }).first().click();
    await expect(page.getByText(/review_status/i).first()).toBeVisible({ timeout: 20000 });
    await expect(page.getByText(/"approved"/i).first()).toBeVisible({ timeout: 20000 });
  });
});
