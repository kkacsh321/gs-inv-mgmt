import { expect, test } from "@playwright/test";

test.describe("Admin go-live flow", () => {
  test("loads go-live evidence and sign-off surfaces", async ({ page }) => {
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

    await page.goto(withCurrentAuth("/Admin"));
    const adminHeader = page.getByRole("heading", { name: /^Admin$/i }).first();
    const usersHeader = page.getByRole("heading", { name: /User Directory/i }).first();
    await expect
      .poll(async () => {
        const adminVisible = await adminHeader.isVisible().catch(() => false);
        const usersVisible = await usersHeader.isVisible().catch(() => false);
        return adminVisible || usersVisible;
      }, { timeout: 15000 })
      .toBeTruthy();

    await page.getByRole("tab", { name: /Governance Exports/i }).first().click();
    await expect(page.getByText(/Go-Live Evidence Pack/i).last()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Go-Live Readiness Score/i).last()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Go-Live Section Sign-Off Tracker/i).last()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Commerce Legal Sign-Off Tracker/i).last()).toBeVisible({ timeout: 15000 });
  });
});
