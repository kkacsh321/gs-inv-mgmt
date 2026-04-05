import { expect, test } from "@playwright/test";

test.describe("Sync flow", () => {
  test("loads sync controls and exception queue surface", async ({ page }) => {
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

    await page.goto(withCurrentAuth("/Sync"));
    await expect(page.getByRole("heading", { name: /^Sync$/i }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole("heading", { name: /Sync Job Controls/i }).first()).toBeVisible({ timeout: 15000 });

    const executeNow = page.getByRole("button", { name: /Execute Selected Job Now/i }).first();
    await expect(executeNow).toBeVisible({ timeout: 15000 });

    if (await executeNow.isEnabled().catch(() => false)) {
      await executeNow.click();
      await expect
        .poll(async () => {
          const ok = await page
            .getByText(/Run #\d+ completed with status/i)
            .first()
            .isVisible()
            .catch(() => false);
          const disabled = await page
            .getByText(/is disabled by configuration/i)
            .first()
            .isVisible()
            .catch(() => false);
          const failed = await page
            .getByText(/Execute-now failed:/i)
            .first()
            .isVisible()
            .catch(() => false);
          return ok || disabled || failed;
        }, { timeout: 20000 })
        .toBeTruthy();
    } else {
      await expect(executeNow).toBeDisabled();
    }

    const exceptionQueue = page.getByRole("heading", { name: /Exception Queue/i }).first();
    await expect(exceptionQueue).toBeVisible({ timeout: 15000 });
  });
});
