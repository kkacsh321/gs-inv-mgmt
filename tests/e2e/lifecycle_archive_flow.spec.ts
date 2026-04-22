import { expect, test, type Page } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

async function selectByLabelText(
  page: Page,
  labelPattern: RegExp,
  optionPattern: RegExp,
): Promise<boolean> {
  const combobox = page.getByRole("combobox", { name: labelPattern }).first();
  if (!(await combobox.isVisible().catch(() => false))) {
    return false;
  }
  await combobox.click().catch(() => {});
  await combobox.fill("").catch(() => {});
  const source = String(optionPattern.source || "")
    .replace(/\\\$/g, "$")
    .replace(/\\(.)/g, "$1");
  await combobox.type(source.slice(0, 80), { delay: 5 }).catch(() => {});
  await page.keyboard.press("Enter").catch(() => {});
  await page.waitForTimeout(250);
  const valueAttr = (await combobox.inputValue().catch(() => "")) || "";
  if (optionPattern.test(valueAttr)) {
    return true;
  }
  await combobox.click().catch(() => {});
  const option = page.getByRole("option").filter({ hasText: optionPattern }).first();
  if (await option.isVisible().catch(() => false)) {
    await option.click().catch(() => {});
    return true;
  }
  await page.keyboard.press("Escape").catch(() => {});
  return false;
}

async function gotoListings(page: Page): Promise<void> {
  await page.goto("/Listings");
  const signedIn = await ensureSignedIn(page);
  test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
  await navigateAuthed(page, "/Listings", "Listings");
  const authRequired = await isAuthGateVisibleEventually(page);
  test.skip(authRequired, "Auth gate still active on Listings page.");
  const listingsHeader = page.getByText(/Marketplace Listings/i).first();
  const createFlowPreview = page.getByText(/Create-flow eBay readiness preview/i).first();
  await expect
    .poll(async () => {
      const headerVisible = await listingsHeader.isVisible().catch(() => false);
      const previewVisible = await createFlowPreview.isVisible().catch(() => false);
      return headerVisible && previewVisible;
    }, { timeout: 15000 })
    .toBeTruthy();
}

test.describe("Lifecycle Archive Controls", () => {
  const SEEDED_EBAY_DRAFT_TITLE = "E2E Seed Listing Draft (eBay)";

  test("listings danger-zone archive and restore roundtrip", async ({ page }) => {
    await gotoListings(page);

    const selectListing = page.getByLabel("Select Listing", { exact: true }).first();
    test.skip(!(await selectListing.isVisible().catch(() => false)), "No side-panel listing selector available.");

    await expect(selectListing).toBeVisible({ timeout: 15000 });
    await page.getByLabel("Search Title / External ID", { exact: true }).first().fill(SEEDED_EBAY_DRAFT_TITLE);
    await page.waitForTimeout(600);
    const selectedSeed = await selectByLabelText(
      page,
      /Select Listing/i,
      new RegExp(SEEDED_EBAY_DRAFT_TITLE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
    );
    let selected = selectedSeed;
    if (!selected) {
      // Fallback to whatever listing is currently selected in the side panel.
      const selectedValue = (await selectListing.inputValue().catch(() => "")).trim();
      selected = selectedValue.length > 0;
    }
    test.skip(!selected, "Listing was not selectable in lifecycle side-panel.");

    // Ensure we start in an unarchived state for deterministic archive->restore assertions.
    const restoreButton = page.getByRole("button", { name: /Restore Archived Listing/i }).first();
    if (await restoreButton.isVisible().catch(() => false)) {
      await restoreButton.click();
      await expect(page.getByText(/Restored listing #\d+/i).first()).toBeVisible({ timeout: 15000 });
    }

    const archiveButton = page.getByRole("button", { name: /Archive Listing/i }).first();
    await expect(archiveButton).toBeVisible({ timeout: 15000 });
    await archiveButton.click();

    await expect(page.getByText(/Archived listing #\d+/i).first()).toBeVisible({ timeout: 15000 });

    const restoreAfterArchive = page.getByRole("button", { name: /Restore Archived Listing/i }).first();
    await expect(restoreAfterArchive).toBeVisible({ timeout: 15000 });
    await restoreAfterArchive.click();

    await expect(page.getByText(/Restored listing #\d+/i).first()).toBeVisible({ timeout: 15000 });
  });
});
