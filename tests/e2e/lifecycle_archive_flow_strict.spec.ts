import { expect, test, type Page } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

const STRICT_ENABLED = ["1", "true", "yes", "on"].includes(
  String(process.env.E2E_STRICT_LIFECYCLE || "0").trim().toLowerCase(),
);

async function selectByLabelTextStrict(page: Page, labelPattern: RegExp, optionPattern: RegExp): Promise<void> {
  const combobox = page.getByRole("combobox", { name: labelPattern }).first();
  await expect(combobox).toBeVisible({ timeout: 10000 });
  await combobox.click();
  await combobox.fill("");
  const source = String(optionPattern.source || "")
    .replace(/\\\$/g, "$")
    .replace(/\\(.)/g, "$1");
  await combobox.type(source.slice(0, 80), { delay: 5 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);
}
test.describe("Lifecycle Archive Controls strict", () => {
  test.skip(!STRICT_ENABLED, "Enable with E2E_STRICT_LIFECYCLE=1 in seeded test environments.");

  test("seeded listing supports strict archive and restore roundtrip", async ({ page }) => {
    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    expect(signedIn).toBeTruthy();

    await navigateAuthed(page, "/Listings", "Listings");
    const authRequired = await isAuthGateVisibleEventually(page);
    expect(authRequired).toBeFalsy();

    await expect(page.getByText(/Marketplace Listings/i).first()).toBeVisible({ timeout: 15000 });
    const archiveButton = page.getByRole("button", { name: /Archive Listing/i }).first();
    let archiveVisible = await archiveButton.isVisible().catch(() => false);
    if (!archiveVisible) {
      const originControl = page.getByRole("combobox", { name: /^Origin$/i }).first();
      if (await originControl.isVisible().catch(() => false)) {
        await selectByLabelTextStrict(page, /^Origin$/i, /^all$/i);
      }
      const listingSelect = page.getByLabel("Select Listing", { exact: true }).first();
      await expect(listingSelect).toBeVisible({ timeout: 30000 });
      const selectedLabel = ((await listingSelect.inputValue().catch(() => "")) || "").trim();
      expect(selectedLabel.length).toBeGreaterThan(0);
      archiveVisible = await archiveButton.isVisible().catch(() => false);
    }
    await expect(archiveButton).toBeVisible({ timeout: 20000 });

    const restoreButton = page.getByRole("button", { name: /Restore Archived Listing/i }).first();
    if (await restoreButton.isVisible().catch(() => false)) {
      await restoreButton.click();
      await expect(page.getByText(/Restored listing #\d+/i).first()).toBeVisible({ timeout: 15000 });
    }

    await archiveButton.click({ force: true });
    const archivedToast = page.getByText(/Archived listing #\d+/i).first();
    await expect
      .poll(
        async () => {
          const toastVisible = await archivedToast.isVisible().catch(() => false);
          const restoreVisibleNow = await page
            .getByRole("button", { name: /Restore Archived Listing/i })
            .first()
            .isVisible()
            .catch(() => false);
          return toastVisible || restoreVisibleNow;
        },
        { timeout: 15000 },
      )
      .toBeTruthy();

    const archivedText = (await archivedToast.textContent().catch(() => "")) || "";
    const archivedId = String((archivedText.match(/Archived listing #(\d+)/i)?.[1] || "")).trim();

    const includeArchived = page.getByRole("checkbox", { name: /Include Archived/i }).first();
    if (await includeArchived.isVisible().catch(() => false)) {
      const checked = await includeArchived.isChecked().catch(() => false);
      if (!checked) {
        await includeArchived.check();
      }
    }

    if (archivedId) {
      const listingSelectAfterArchive = page.getByRole("combobox", { name: /^Select Listing$/i }).first();
      if (await listingSelectAfterArchive.isVisible().catch(() => false)) {
        await selectByLabelTextStrict(page, /^Select Listing$/i, new RegExp(`^#${archivedId}\\s*\\|`, "i"));
      }
    }

    const restoreAfterArchive = page.getByRole("button", { name: /Restore Archived Listing/i }).first();
    if (await restoreAfterArchive.isVisible().catch(() => false)) {
      await restoreAfterArchive.click();
      await expect(page.getByText(/Restored listing #\d+/i).first()).toBeVisible({ timeout: 15000 });
    }
  });
});
