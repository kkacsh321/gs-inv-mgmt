import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

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

    await page.goto("/Coin_Intake_Wizard");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Coin_Intake_Wizard", "Coin Intake Wizard");
    const coinAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(coinAuthRequired, "Auth gate still active on Coin Intake Wizard page.");
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
      await navigateAuthed(page, "/Coin_Intake_Wizard", "Coin Intake Wizard");
    }
    await expect(wizardHeader).toBeVisible({ timeout: 15000 });
    await expect(wizardRunButton).toBeVisible({ timeout: 15000 });

    const uniqueSku = `E2E-COIN-${Date.now()}`;
    await page.getByLabel("SKU", { exact: true }).first().fill(uniqueSku);
    await page.getByLabel("Product Title", { exact: true }).first().fill(`E2E Coin ${uniqueSku}`);
    await page.getByRole("button", { name: /Run Intake Wizard/i }).first().click();
    await expect(page.getByText(/Created product #\d+/i).first()).toBeVisible({ timeout: 20000 });
  });

  test("requires eBay purchase item id when Purchased On eBay is enabled", async ({ page }) => {
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

    await page.goto("/Coin_Intake_Wizard");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Coin_Intake_Wizard", "Coin Intake Wizard");
    const coinAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(coinAuthRequired, "Auth gate still active on Coin Intake Wizard page.");

    const purchasedOnEbay = page.getByLabel("Purchased On eBay", { exact: true }).first();
    await expect(purchasedOnEbay).toBeVisible({ timeout: 15000 });

    const itemIdField = page.getByLabel("eBay Purchase Item ID", { exact: true });
    const linkField = page.getByLabel("eBay Purchase Link", { exact: true });
    await expect(itemIdField).toHaveCount(0);
    await expect(linkField).toHaveCount(0);

    await purchasedOnEbay.check();
    await expect(itemIdField.first()).toBeVisible({ timeout: 10000 });
    await expect(linkField.first()).toBeVisible({ timeout: 10000 });

    const uniqueSku = `E2E-COIN-EBAY-${Date.now()}`;
    await page.getByLabel("SKU", { exact: true }).first().fill(uniqueSku);
    await page.getByLabel("Product Title", { exact: true }).first().fill(`E2E Coin ${uniqueSku}`);
    await page.getByRole("button", { name: /Run Intake Wizard/i }).first().click();
    await expect(
      page.getByText(/eBay Purchase Item ID is required when Purchased On eBay is enabled\./i).first(),
    ).toBeVisible({ timeout: 10000 });
  });
});
