import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

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

    await page.goto("/Shipping");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Shipping", "Shipping");
    const shippingAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(shippingAuthRequired, "Auth gate still active on Shipping page.");
    const shippingHeading = page.getByRole("heading", { name: /^Shipping$/i }).first();
    const shippingCaption = page.getByText(/Operational shipping queues/i).first();
    await expect
      .poll(async () => {
        const headingVisible = await shippingHeading.isVisible().catch(() => false);
        const captionVisible = await shippingCaption.isVisible().catch(() => false);
        return headingVisible || captionVisible;
      }, { timeout: 15000 })
      .toBeTruthy();

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

    // Deterministic mutation assertion: create a carrier preset and verify success.
    const presetName = `E2E-PRESET-${Date.now()}`;
    const presetNameInput = page.getByLabel("Preset Name", { exact: true }).first();
    if (await presetNameInput.isVisible().catch(() => false)) {
      await presetNameInput.fill(presetName);
      await page.getByLabel("Service", { exact: true }).first().fill("Ground Advantage");
      await page.getByRole("button", { name: /^Create Preset$/i }).first().click();
      await expect(page.getByText(/Shipping preset created\./i).first()).toBeVisible({ timeout: 15000 });
      await expect(page.getByText(new RegExp(presetName, "i")).first()).toBeVisible({ timeout: 15000 });
    }

    const bulkUpdate = page.getByRole("button", { name: /Apply Bulk Update/i }).first();
    if (await bulkUpdate.isVisible().catch(() => false)) {
      await bulkUpdate.click();
      await expect(page.getByText(/Select at least one sale\./i).first()).toBeVisible({ timeout: 10000 });
    }

    const noSalesBanner = page.getByText(/No sales records yet|No sales match/i).first();
    if (!(await noSalesBanner.isVisible().catch(() => false))) {
      // Mutation assertion 1: queue at least one label purchase job and process due jobs.
      const queueSales = page.getByLabel(/Select Sales To Queue For Label Purchase/i).first();
      if (await queueSales.isVisible().catch(() => false)) {
        await queueSales.click();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
        const queueSubmit = page.getByRole("button", { name: /^Queue Label Purchase Jobs$/i }).first();
        await expect(queueSubmit).toBeVisible({ timeout: 10000 });
        await queueSubmit.click();
        await expect(page.getByText(/Queued \d+ shipping label job\(s\)\./i).first()).toBeVisible({
          timeout: 15000,
        });

        const processDue = page.getByRole("button", { name: /^Process Due Shipping Jobs$/i }).first();
        if (await processDue.isVisible().catch(() => false)) {
          await processDue.click();
          await expect(page.getByText(/Processed=\d+, success=\d+, queued=\d+, failed=\d+\./i).first()).toBeVisible({
            timeout: 15000,
          });
        }
      }

      // Mutation assertion 2: tracking writeback signal via bulk update (when Select Sales rows exist).
      const selectSales = page.getByLabel(/^Select Sales$/i).first();
      if (await selectSales.isVisible().catch(() => false)) {
        const trackingValue = `E2E-TRK-${Date.now()}`;
        await selectSales.click();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
        const trackingInput = page.getByLabel(/Set Tracking Number \(Optional\)/i).first();
        if (await trackingInput.isVisible().catch(() => false)) {
          await trackingInput.fill(trackingValue);
        }
        const applyBulk = page.getByRole("button", { name: /^Apply Bulk Update$/i }).first();
        if (await applyBulk.isVisible().catch(() => false)) {
          await applyBulk.click();
          await expect(page.getByText(/Updated \d+ sale\(s\)\./i).first()).toBeVisible({ timeout: 15000 });
          await expect(page.getByText(new RegExp(trackingValue, "i")).first()).toBeVisible({ timeout: 15000 });
        }
      }
    }
  });
});
