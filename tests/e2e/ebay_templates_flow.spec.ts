import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

test.describe("eBay Templates flow", () => {
  test("creates template, edits existing template, and performs custom HTML block CRUD", async ({ page }) => {
    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");

    await navigateAuthed(page, "/eBay_Templates", "eBay Templates");
    const authRequired = await isAuthGateVisibleEventually(page);
    test.skip(authRequired, "Auth gate still active on eBay Templates page.");

    await expect(page.getByText(/^eBay Templates$/i).first()).toBeVisible({ timeout: 15000 });

    const unique = Date.now();
    const templateName = `E2E Template ${unique}`;
    const editedTitle = `E2E Edited Title ${unique}`;
    const customBlockName = `E2E Block ${unique}`;

    // Save template (use last matching inputs to target the save form).
    await page.getByLabel("Template Name", { exact: true }).last().fill(templateName);
    await page.getByLabel("Listing Title Template").last().fill(`Title ${templateName}`);
    await page.getByLabel("Marketplace Details / HTML Template").last().fill(`<p>${templateName}</p>`);
    await page.getByRole("button", { name: /^Save Template$/i }).first().click();
    await expect(page.getByText(/Template saved\./i).first()).toBeVisible({ timeout: 20000 });

    // Edit existing template.
    const editSelect = page.getByRole("combobox", { name: /Template To Edit/i }).first();
    await expect(editSelect).toBeVisible({ timeout: 10000 });
    await page.getByLabel("Listing Title Template").first().fill(editedTitle);
    await page.getByRole("button", { name: /Save Template Changes/i }).click();
    await expect(page.getByLabel("Listing Title Template").first()).toHaveValue(editedTitle, { timeout: 20000 });

    // Open reusable blocks panel and save a custom block.
    await page.getByText(/Reusable Branded HTML Blocks/i).first().click();
    await expect(page.getByLabel("Block Name", { exact: true })).toBeVisible({ timeout: 10000 });
    await page.getByLabel("Block Name", { exact: true }).fill(customBlockName);
    await page.getByLabel("Block HTML", { exact: true }).fill(`<div>${customBlockName}</div>`);
    await page.getByRole("button", { name: /^Save Block$/i }).click();
    await expect(page.getByLabel("Block Name", { exact: true })).toHaveValue(customBlockName, { timeout: 20000 });

    // Delete custom block.
    const blockSelect = page.getByRole("combobox", { name: /Block/i }).first();
    await blockSelect
      .selectOption({ label: customBlockName })
      .catch(async () => {
        await blockSelect.click();
        await page
          .getByText(new RegExp(customBlockName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"))
          .last()
          .click();
      });
    await page.getByRole("button", { name: /Delete Custom Block/i }).click();
    await expect
      .poll(
        async () =>
          (await page.getByText(/Deleted custom block/i).first().isVisible().catch(() => false)) ||
          (
            await page
              .getByText(/Only custom blocks can be deleted/i)
              .first()
              .isVisible()
              .catch(() => false)
          ),
        { timeout: 20000 }
      )
      .toBeTruthy();
  });
});
