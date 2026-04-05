import { expect, test } from "@playwright/test";

test.describe("Coin Intake Wizard", () => {
  test("creates product from coin intake flow", async ({ page }) => {
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

    await page.goto(withCurrentAuth("/Coin_Intake_Wizard"));
    const wizardRunButton = page.getByRole("button", { name: /Run Intake Wizard/i }).first();
    const wizardHeader = page.getByRole("heading", { name: /Coin Intake Wizard/i }).first();
    if (!(await wizardRunButton.isVisible().catch(() => false))) {
      const sidebarWizardLink = page.locator("a[href$='/Coin_Intake_Wizard']").first();
      if (await sidebarWizardLink.isVisible().catch(() => false)) {
        const href = await sidebarWizardLink.getAttribute("href");
        if (href) {
          await page.goto(href);
        } else {
          await sidebarWizardLink.click();
        }
      }
      const stillAuthRequired =
        (await authGateAlert.isVisible().catch(() => false)) ||
        (await signInButton.isVisible().catch(() => false));
      if (stillAuthRequired) {
        await usernameInput.fill(username);
        await passwordInput.fill(password);
        await signInButton.click();
        await expect
          .poll(async () => {
            const visible = await signInButton.isVisible().catch(() => false);
            const required = await authGateAlert.isVisible().catch(() => false);
            return !visible && !required;
          }, { timeout: 15000 })
          .toBeTruthy();
        await page.goto(withCurrentAuth("/Coin_Intake_Wizard"));
      }
    }
    await expect(wizardHeader).toBeVisible({ timeout: 15000 });
    await expect(wizardRunButton).toBeVisible({ timeout: 15000 });

    const uniqueSku = `E2E-COIN-${Date.now()}`;
    await page.getByLabel("SKU", { exact: true }).first().fill(uniqueSku);
    await page.getByLabel("Product Title", { exact: true }).first().fill(`E2E Coin ${uniqueSku}`);
    await page.getByRole("button", { name: /Run Intake Wizard/i }).first().click();
    await expect(page.getByText(/Created product #\d+/i).first()).toBeVisible({ timeout: 20000 });
  });
});
