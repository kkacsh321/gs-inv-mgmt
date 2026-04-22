import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually } from "./_auth";

test.describe("Admin lifecycle retention controls", () => {
  test("runs manual lifecycle cleanup and shows evidence block", async ({ page }) => {
    const adminUsername = process.env.E2E_ADMIN_USERNAME || "admin";
    const adminPassword =
      process.env.E2E_ADMIN_PASSWORD || process.env.E2E_PASSWORD || "e2e-password-123";

    await page.goto("/Admin");
    let signedIn = await ensureSignedIn(page, {
      username: adminUsername,
      password: adminPassword,
    });
    expect(signedIn).toBeTruthy();

    if (await isAuthGateVisibleEventually(page, 2000)) {
      signedIn = await ensureSignedIn(page, {
        username: adminUsername,
        password: adminPassword,
      });
      expect(signedIn).toBeTruthy();
    }

    const adminReady = page.getByRole("heading", { name: /^Admin$/i }).first();
    await expect(adminReady.or(page.getByRole("heading", { name: /User Directory/i }).first())).toBeVisible({
      timeout: 15000,
    });

    const integrationsTab = page.getByRole("tab", { name: /^Integrations$/i }).first();
    await expect(integrationsTab).toBeVisible({ timeout: 15000 });
    await integrationsTab.click();
    await expect(page.getByText(/Lifecycle Archive Retention Controls/i).first()).toBeVisible({ timeout: 15000 });

    const runNow = page.getByRole("button", { name: /^Run Lifecycle Cleanup Now$/i }).first();
    await expect(runNow).toBeVisible({ timeout: 15000 });
    await runNow.click({ force: true });

    // Streamlit rerun may reset tab selection; navigate back to Integrations and assert evidence.
    await expect(page.getByRole("tab", { name: /^Integrations$/i }).first()).toBeVisible({ timeout: 15000 });
    await page.getByRole("tab", { name: /^Integrations$/i }).first().click();

    await expect(page.getByText(/Last Lifecycle Cleanup Run/i).first()).toBeVisible({ timeout: 15000 });
    const evidenceJson = page.locator("pre").filter({ hasText: '"status": "success"' }).first();
    await expect(evidenceJson).toBeVisible({ timeout: 15000 });
    await expect(page.locator("pre").filter({ hasText: '"deleted_archived_media"' }).first()).toBeVisible({
      timeout: 15000,
    });
  });
});
