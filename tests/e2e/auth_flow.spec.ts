import { expect, test } from "@playwright/test";

test.describe("Auth flow", () => {
  test("signs in and signs out when password auth is enabled", async ({ page }) => {
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

    const signInRequiredBanner = page.getByText(/Sign in required to access app pages/i).first();
    const signInButton = page.getByRole("button", { name: /Sign In/i }).first();
    await page.waitForTimeout(800);
    const authRequired =
      (await signInRequiredBanner.isVisible().catch(() => false)) ||
      (await signInButton.isVisible().catch(() => false));
    test.skip(!authRequired, "Password auth is disabled in this environment.");

    const username = process.env.E2E_USERNAME || "e2e";
    const password = process.env.E2E_PASSWORD || "";
    test.skip(!username || !password, "Auth is enabled locally; set E2E_USERNAME/E2E_PASSWORD to run this test.");

    await page.getByLabel("Username", { exact: true }).first().fill(username);
    const passwordInput = page.getByLabel("Password", { exact: true }).first();
    await passwordInput.fill(password);
    await expect(passwordInput).toHaveValue(password);
    const rememberCheckbox = page.getByLabel("Remember me on this browser").first();
    const rememberVisible = await rememberCheckbox.isVisible().catch(() => false);
    if (rememberVisible) {
      await rememberCheckbox.check();
    } else {
      // Streamlit theme/layout can hide native checkbox input; click label text fallback.
      await page.getByText("Remember me on this browser").first().click({ force: true });
    }
    await signInButton.click();
    await page.waitForTimeout(300);
    const invalidLogin = page.getByText(/Invalid username\/password/i).first();
    if (await invalidLogin.isVisible().catch(() => false)) {
      throw new Error("E2E auth credentials rejected after seed.");
    }

    const signOutButton = page.getByRole("button", { name: /Sign Out/i }).first();
    const signedInBanner = page.getByText(/Signed in as/i).first();
    await expect
      .poll(async () => {
        const signOutVisible = await signOutButton.isVisible().catch(() => false);
        const signedInVisible = await signedInBanner.isVisible().catch(() => false);
        return signOutVisible || signedInVisible;
      }, { timeout: 15000 })
      .toBeTruthy();
    await expect(signedInBanner).toBeVisible({ timeout: 15000 });
    if (await signOutButton.isVisible().catch(() => false)) {
      await signOutButton.click();
      await expect(signInButton).toBeVisible({ timeout: 15000 });
    }
  });
});
