import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

const STRICT_ENABLED = ["1", "true", "yes", "on"].includes(
  String(process.env.E2E_STRICT_LISTINGS || "0").trim().toLowerCase(),
);
const SEEDED_EBAY_DRAFT_TITLE = "E2E Seed Listing Draft (eBay)";

const selectByLabelTextStrict = async (
  page: import("@playwright/test").Page,
  labelPattern: RegExp,
  optionPattern: RegExp,
): Promise<void> => {
  const labeledInput = page.getByLabel(labelPattern).first();
  const roleCombobox = page.getByRole("combobox", { name: labelPattern }).first();
  let combobox = labeledInput;
  if (!(await labeledInput.isVisible().catch(() => false))) {
    combobox = roleCombobox;
  }
  await expect(combobox).toBeVisible({ timeout: 10000 });
  await combobox.click();
  await combobox.fill("");
  const source = String(optionPattern.source || "")
    .replace(/\\\$/g, "$")
    .replace(/\\(.)/g, "$1");
  await combobox.type(source.slice(0, 80), { delay: 5 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);

  const valueAttr = (await combobox.inputValue().catch(() => "")) || "";
  if (optionPattern.test(valueAttr)) {
    return;
  }
  await combobox.click();
  const option = page.getByRole("option").filter({ hasText: optionPattern }).first();
  await expect(option).toBeVisible({ timeout: 5000 });
  await option.click();
};

test.describe("Listings flow strict", () => {
  test.skip(!STRICT_ENABLED, "Enable with E2E_STRICT_LISTINGS=1 in seeded test environments.");

  test("seeded listing supports eBay preflight controls", async ({ page }) => {
    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    expect(signedIn).toBeTruthy();

    await navigateAuthed(page, "/Listings", "Listings");
    const listingsAuthRequired = await isAuthGateVisibleEventually(page);
    expect(listingsAuthRequired).toBeFalsy();

    const listingsHeader = page.getByText(/Marketplace Listings/i).first();
    await expect(listingsHeader).toBeVisible({ timeout: 15000 });
    const seededPattern = new RegExp(SEEDED_EBAY_DRAFT_TITLE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");

    await selectByLabelTextStrict(page, /Choose Listing/i, seededPattern);
    const postMode = page.getByRole("combobox", { name: /eBay Post Mode/i }).first();
    await expect(postMode).toBeVisible({ timeout: 10000 });
    await selectByLabelTextStrict(page, /eBay Post Mode/i, /Save Unpublished Offer \(API Draft\)/i);
    await expect(postMode).toHaveAttribute(
      "aria-label",
      /Selected Save Unpublished Offer \(API Draft\)\. eBay Post Mode/i,
      { timeout: 10000 },
    );

    const runPreflightBtn = page.getByRole("button", { name: /^Run eBay Dependency Preflight$/i }).first();
    await expect(runPreflightBtn).toBeVisible({ timeout: 10000 });
    await expect(runPreflightBtn).toBeEnabled({ timeout: 10000 });
    await runPreflightBtn.click();
    await expect
      .poll(
        async () => {
          const headingVisible = await page
            .getByRole("heading", { name: /^eBay Dependency Preflight$/i })
            .first()
            .isVisible()
            .catch(() => false);
          const blockerVisible = await page
            .getByText(/eBay dependency preflight found blockers/i)
            .first()
            .isVisible()
            .catch(() => false);
          const warnVisible = await page
            .getByText(/eBay dependency preflight completed with warnings/i)
            .first()
            .isVisible()
            .catch(() => false);
          const passVisible = await page
            .getByText(/eBay dependency preflight passed/i)
            .first()
            .isVisible()
            .catch(() => false);
          return headingVisible || blockerVisible || warnVisible || passVisible;
        },
        { timeout: 15000 },
      )
      .toBeTruthy();
  });

  test("seeded listing supports publish draft save/resume/clear controls", async ({ page }) => {
    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    expect(signedIn).toBeTruthy();

    await navigateAuthed(page, "/Listings", "Listings");
    const listingsAuthRequired = await isAuthGateVisibleEventually(page);
    expect(listingsAuthRequired).toBeFalsy();

    await expect(page.getByText(/Marketplace Listings/i).first()).toBeVisible({ timeout: 15000 });
    const seededPattern = new RegExp(SEEDED_EBAY_DRAFT_TITLE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");
    await selectByLabelTextStrict(page, /Choose Listing/i, seededPattern);

    const saveBtn = page.getByRole("button", { name: /^Save Publish Draft$/i }).first();
    const resumeBtn = page.getByRole("button", { name: /^Resume Publish Draft$/i }).first();
    const clearBtn = page.getByRole("button", { name: /^Clear Publish Draft$/i }).first();
    await expect(saveBtn).toBeVisible({ timeout: 10000 });
    await expect(resumeBtn).toBeVisible({ timeout: 10000 });
    await expect(clearBtn).toBeVisible({ timeout: 10000 });

    await saveBtn.click();
    await expect(page.getByText(/Saved publish draft\./i).first()).toBeVisible({ timeout: 15000 });

    await resumeBtn.click();
    await expect(page.getByText(/Resumed publish draft\./i).first()).toBeVisible({ timeout: 15000 });

    await clearBtn.click();
    await expect(page.getByText(/Cleared publish draft\./i).first()).toBeVisible({ timeout: 15000 });
  });
});
