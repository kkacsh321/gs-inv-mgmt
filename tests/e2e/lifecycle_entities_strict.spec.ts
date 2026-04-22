import { expect, test, type Page } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

const STRICT_ENABLED = ["1", "true", "yes", "on"].includes(
  String(process.env.E2E_STRICT_LIFECYCLE || "0").trim().toLowerCase(),
);

async function goAuthed(page: import("@playwright/test").Page, path: string, navLabel: string, ready: RegExp) {
  await page.goto(path);
  const signedIn = await ensureSignedIn(page);
  expect(signedIn).toBeTruthy();
  await navigateAuthed(page, path, navLabel);
  const authRequired = await isAuthGateVisibleEventually(page);
  expect(authRequired).toBeFalsy();
  await expect(page.getByText(ready).first()).toBeVisible({ timeout: 20000 });
}

async function selectByLabelTextStrict(page: Page, labelPattern: RegExp, query: string): Promise<void> {
  const combobox = page.getByRole("combobox", { name: labelPattern }).first();
  await expect(combobox).toBeVisible({ timeout: 10000 });
  await combobox.click();
  await combobox.fill("");
  await combobox.type(String(query || "").slice(0, 80), { delay: 5 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);
}

async function selectedEntityId(combobox: import("@playwright/test").Locator): Promise<string> {
  const text = ((await combobox.textContent().catch(() => "")) || "").trim();
  return String(text.match(/#(\d+)/)?.[1] || "").trim();
}

async function ensureIncludeArchivedOn(page: Page, labelPattern: RegExp): Promise<void> {
  const checkbox = page.getByRole("checkbox", { name: labelPattern }).first();
  const checked = await checkbox.isChecked().catch(() => false);
  if (checked) return;
  const label = page.locator("label").filter({ hasText: labelPattern }).first();
  await expect(label).toBeVisible({ timeout: 10000 });
  await label.click({ force: true });
  await page.waitForTimeout(250);
}

test.describe("Lifecycle strict entities", () => {
  test.skip(!STRICT_ENABLED, "Enable with E2E_STRICT_LIFECYCLE=1 in seeded test environments.");

  test("Products archive/restore roundtrip", async ({ page }) => {
    await goAuthed(page, "/Products", "Products", /Products/i);

    const productSelect = page.getByRole("combobox", { name: /Select Product/i }).first();
    await expect(productSelect).toBeVisible({ timeout: 15000 });
    const productId = await selectedEntityId(productSelect);

    await ensureIncludeArchivedOn(page, /Include Archived/i);

    const forceArchive = page.getByRole("checkbox", { name: /Force archive even with active listings/i }).first();
    if (await forceArchive.isVisible().catch(() => false)) {
      const forceChecked = await forceArchive.isChecked().catch(() => false);
      if (!forceChecked) {
        await forceArchive.check({ force: true });
      }
    }

    await expect(page.getByRole("button", { name: /^Archive Product$/i }).first()).toBeVisible({ timeout: 15000 });

    const archiveButton = page.getByRole("button", { name: /^Archive Product$/i }).first();
    await archiveButton.click({ force: true });
    await expect
      .poll(
        async () =>
          (await page.getByRole("button", { name: /^Restore Product$/i }).first().isVisible().catch(() => false)) ||
          (await page.getByText(/Archived product #\d+\./i).first().isVisible().catch(() => false)),
        { timeout: 15000 },
      )
      .toBeTruthy();

    if (productId) {
      await selectByLabelTextStrict(page, /Select Product/i, `#${productId}`);
    }
    const restoreButton = page.getByRole("button", { name: /^Restore Product$/i }).first();
    await expect(restoreButton).toBeVisible({ timeout: 15000 });
    await restoreButton.click({ force: true });
    await expect
      .poll(
        async () =>
          (await page.getByRole("button", { name: /^Archive Product$/i }).first().isVisible().catch(() => false)) ||
          (await page.getByText(/Restored product #\d+\./i).first().isVisible().catch(() => false)),
        { timeout: 15000 },
      )
      .toBeTruthy();
  });

  test("Lots archive/restore roundtrip", async ({ page }) => {
    await goAuthed(page, "/Lots", "Lots", /Lots/i);

    const lotSelect = page.getByRole("combobox", { name: /Select Lot/i }).first();
    await expect(lotSelect).toBeVisible({ timeout: 15000 });
    const lotId = await selectedEntityId(lotSelect);

    const restoreButton = page.getByRole("button", { name: /^Restore Lot$/i }).first();
    if (await restoreButton.isVisible().catch(() => false)) {
      await restoreButton.click();
      await expect
        .poll(
          async () =>
            (await page.getByText(/Restored lot #\d+\./i).first().isVisible().catch(() => false)) ||
            (await page.getByRole("button", { name: /^Archive Lot$/i }).first().isVisible().catch(() => false)),
          { timeout: 15000 },
        )
        .toBeTruthy();
    }

    const archiveButton = page.getByRole("button", { name: /^Archive Lot$/i }).first();
    await expect(archiveButton).toBeVisible({ timeout: 15000 });
    await archiveButton.click({ force: true });
    await page.waitForTimeout(400);

    const includeArchivedLots = page.getByRole("checkbox", { name: /Include Archived Lots/i }).first();
    if (await includeArchivedLots.isVisible().catch(() => false)) {
      const checked = await includeArchivedLots.isChecked().catch(() => false);
      if (!checked) await includeArchivedLots.check();
    }
    if (lotId) {
      await selectByLabelTextStrict(page, /Select Lot/i, `#${lotId}`);
    }

    const restoreAfterArchive = page.getByRole("button", { name: /^Restore Lot$/i }).first();
    await expect(restoreAfterArchive).toBeVisible({ timeout: 15000 });
    await restoreAfterArchive.click();
    await expect
      .poll(
        async () =>
          (await page.getByText(/Restored lot #\d+\./i).first().isVisible().catch(() => false)) ||
          (await page.getByRole("button", { name: /^Archive Lot$/i }).first().isVisible().catch(() => false)),
        { timeout: 15000 },
      )
      .toBeTruthy();
  });

  test("Media archive/restore roundtrip", async ({ page }) => {
    await goAuthed(page, "/Media", "Media", /Media Library/i);

    const mediaSelect = page.getByRole("combobox", { name: /Select Media to Archive\/Restore/i }).first();
    await expect(mediaSelect).toBeVisible({ timeout: 15000 });
    const mediaId = await selectedEntityId(mediaSelect);

    await ensureIncludeArchivedOn(page, /^Include Archived$/i);

    const archiveButton = page.getByRole("button", { name: /^Archive Media$/i }).first();
    await expect(archiveButton).toBeVisible({ timeout: 15000 });
    await archiveButton.click({ force: true });
    await expect
      .poll(
        async () =>
          (await page.getByRole("button", { name: /^Restore Media$/i }).first().isVisible().catch(() => false)) ||
          (await page.getByText(/^Media archived\.$/i).first().isVisible().catch(() => false)),
        { timeout: 15000 },
      )
      .toBeTruthy();

    if (mediaId) {
      await selectByLabelTextStrict(page, /Select Media to Archive\/Restore/i, `#${mediaId}`);
    }
    const restoreButton = page.getByRole("button", { name: /^Restore Media$/i }).first();
    await expect(restoreButton).toBeVisible({ timeout: 15000 });
    await restoreButton.click({ force: true });
    await expect
      .poll(
        async () =>
          (await page.getByRole("button", { name: /^Archive Media$/i }).first().isVisible().catch(() => false)) ||
          (await page.getByText(/^Media restored\.$/i).first().isVisible().catch(() => false)),
        { timeout: 15000 },
      )
      .toBeTruthy();
  });
});
