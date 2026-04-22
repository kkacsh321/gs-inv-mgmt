import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually } from "./_auth";

test.describe("Admin go-live flow", () => {
  test("loads go-live evidence and sign-off surfaces", async ({ page }) => {
    const adminUsername = process.env.E2E_ADMIN_USERNAME || "admin";
    const adminPassword =
      process.env.E2E_ADMIN_PASSWORD || process.env.E2E_PASSWORD || "e2e-password-123";

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
    const adminHeader = page.getByRole("heading", { name: /^Admin$/i }).first();
    const usersHeader = page.getByRole("heading", { name: /User Directory/i }).first();

    let adminReady = false;
    for (let i = 0; i < 20; i += 1) {
      const adminVisible = await adminHeader.isVisible().catch(() => false);
      const usersVisible = await usersHeader.isVisible().catch(() => false);
      if (adminVisible || usersVisible) {
        adminReady = true;
        break;
      }
      await page.waitForTimeout(250);
    }
    expect(adminReady).toBeTruthy();

    const governanceTab = page.getByRole("tab", { name: /Governance Exports/i }).first();
    await expect(governanceTab).toBeVisible({ timeout: 15000 });
    await governanceTab.click();
    await expect(page.getByText(/Go-Live Evidence Pack/i).last()).toBeAttached({ timeout: 15000 });
    await expect(page.getByText(/Go-Live Readiness Score/i).last()).toBeAttached({ timeout: 15000 });
    await expect(page.getByText(/Go-Live Section Sign-Off Tracker/i).last()).toBeAttached({ timeout: 15000 });
    await expect(page.getByText(/Commerce Legal Sign-Off Tracker/i).last()).toBeAttached({ timeout: 15000 });
    await expect(page.getByText(/Lifecycle Retention Policy Sign-Off Tracker/i).last()).toBeAttached({
      timeout: 15000,
    });
    await expect(
      page.getByRole("button", { name: /Download Lifecycle Retention Policy Sign-Off CSV/i }).first(),
    ).toBeVisible({ timeout: 15000 });

    // Deterministic mutation assertion: record one checklist item sign-off.
    const checklistSectionKey = page.getByLabel("Section Key", { exact: true }).first();
    const checklistItemKey = page.getByLabel("Item Key", { exact: true }).first();
    const checklistItemLabel = page.getByLabel("Item Label", { exact: true }).first();
    const checklistEvidence = page.getByLabel("Evidence Link", { exact: true }).first();
    const checklistNotes = page.getByLabel("Notes", { exact: true }).first();
    const recordChecklistBtn = page.getByRole("button", { name: /^Record Checklist Item Sign-Off$/i }).first();

    if (await checklistSectionKey.isVisible().catch(() => false)) {
      const ts = Date.now();
      await checklistSectionKey.fill("e2e");
      await checklistItemKey.fill(`admin_golive_${ts}`);
      await checklistItemLabel.fill(`E2E Admin Go-Live ${ts}`);
      await checklistEvidence.fill(`e2e://admin-go-live/${ts}`);
      await checklistNotes.fill("E2E mutation assertion for governance sign-off tracker.");
      await recordChecklistBtn.click();
      await expect(page.getByText(/Checklist item sign-off recorded\./i).first()).toBeVisible({
        timeout: 15000,
      });
      await expect(page.getByText(new RegExp(`admin_golive_${ts}`, "i")).first()).toBeVisible({
        timeout: 15000,
      });
    }
  });
});
