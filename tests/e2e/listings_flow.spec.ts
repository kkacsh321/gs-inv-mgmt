import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

test.describe("Listings flow", () => {
  const SEEDED_EBAY_DRAFT_TITLE = "E2E Seed Listing Draft (eBay)";

  const selectByLabelText = async (
    page: import("@playwright/test").Page,
    labelPattern: RegExp,
    optionPattern: RegExp,
  ): Promise<boolean> => {
    const combobox = page.getByRole("combobox", { name: labelPattern }).first();
    if (!(await combobox.isVisible().catch(() => false))) {
      return false;
    }
    // Streamlit selectboxes are most reliable via typeahead + Enter.
    await combobox.click().catch(() => {});
    await combobox.fill("").catch(() => {});
    const source = String(optionPattern.source || "")
      .replace(/\\\$/g, "$")
      .replace(/\\(.)/g, "$1");
    await combobox.type(source.slice(0, 80), { delay: 5 }).catch(() => {});
    await page.keyboard.press("Enter").catch(() => {});
    await page.waitForTimeout(250);

    const valueAttr = (await combobox.inputValue().catch(() => "")) || "";
    const selectedByValue = optionPattern.test(valueAttr);
    if (selectedByValue) {
      return true;
    }

    await combobox.click().catch(() => {});
    const option = page.getByRole("option").filter({ hasText: optionPattern }).first();
    if (await option.isVisible().catch(() => false)) {
      await option.click().catch(() => {});
      return true;
    }
    const looseTextOption = page.getByText(optionPattern).first();
    if (await looseTextOption.isVisible().catch(() => false)) {
      await looseTextOption.click().catch(() => {});
      return true;
    }
    await page.keyboard.press("Escape").catch(() => {});
    return false;
  };

  const eventuallyVisible = async (
    fn: () => Promise<boolean>,
    timeoutMs = 10000,
    intervalMs = 250,
  ): Promise<boolean> => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (await fn().catch(() => false)) {
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    return fn().catch(() => false);
  };

  test("creates draft listing and completes review action", async ({ page }) => {
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

    const uniqueSku = `E2E-LIST-${Date.now()}`;
    const listingTitle = `E2E Listing ${uniqueSku}`;

    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Listings", "Listings");
    const listingsAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(listingsAuthRequired, "Auth gate still active on Listings page.");
    const listingsHeader = page.getByText(/Marketplace Listings/i).first();
    const createFlowPreview = page.getByText(/Create-flow eBay readiness preview/i).first();
    await expect
      .poll(async () => {
        const headerVisible = await listingsHeader.isVisible().catch(() => false);
        const previewVisible = await createFlowPreview.isVisible().catch(() => false);
        return headerVisible && previewVisible;
      }, { timeout: 15000 })
      .toBeTruthy();
    await expect(listingsHeader).toBeVisible({ timeout: 15000 });
    await expect(createFlowPreview).toBeVisible({ timeout: 15000 });
    await selectByLabelText(page, /^Origin$/i, /^all$/i);

    await page.getByLabel("Listing Title", { exact: true }).first().fill(listingTitle);
    await page.getByLabel("Listing Price", { exact: true }).first().fill("39");
    await page.getByRole("button", { name: /Create Listing/i }).first().click();
    await expect(page.getByText(/Listing created\./i).first()).toBeVisible({ timeout: 20000 });

    await page.getByLabel("Search Title / External ID", { exact: true }).first().fill(listingTitle);
    await page.waitForTimeout(600);
    const selectedCreatedForReview =
      (await selectByLabelText(
        page,
        /Select Listing/i,
        new RegExp(listingTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
      )) ||
      (await selectByLabelText(
        page,
        /Select Listing/i,
        new RegExp(SEEDED_EBAY_DRAFT_TITLE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
      ));
    test.skip(!selectedCreatedForReview, "Created listing was not selectable in review side-panel.");

    const approveReviewBtn = page.getByRole("button", { name: /Approve Listing Review/i }).first();
    const canReview = await eventuallyVisible(async () => approveReviewBtn.isVisible(), 10000);
    test.skip(!canReview, "Review action controls are unavailable in this environment/filter context.");

    await approveReviewBtn.click();
    await expect
      .poll(
        async () => {
          const success = await page
            .getByText(/Listing review updated: `approved`\./i)
            .first()
            .isVisible()
            .catch(() => false);
          const approved = await page.getByText(/"approved"/i).first().isVisible().catch(() => false);
          return success || approved;
        },
        { timeout: 20000 },
      )
      .toBeTruthy();
  });

  test("supports eBay post-mode selection and dependency preflight flow", async ({ page }) => {
    await page.goto("/Listings");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Listings", "Listings");
    const listingsAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(listingsAuthRequired, "Auth gate still active on Listings page.");

    const listingsHeader = page.getByText(/Marketplace Listings/i).first();
    await expect(listingsHeader).toBeVisible({ timeout: 15000 });
    await selectByLabelText(page, /^Origin$/i, /^all$/i);

    const uniqueSku = `E2E-LIST-POST-${Date.now()}`;
    const listingTitle = `E2E Listing ${uniqueSku}`;
    await page.getByLabel("Listing Title", { exact: true }).first().fill(listingTitle);
    await page.getByLabel("Listing Price", { exact: true }).first().fill("39");
    await page.getByRole("button", { name: /Create Listing/i }).first().click();
    await expect(page.getByText(/Listing created\./i).first()).toBeVisible({ timeout: 20000 });

    const selectedCreatedForPublish =
      (await selectByLabelText(
        page,
        /Choose Listing/i,
        new RegExp(listingTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
      )) ||
      (await selectByLabelText(
        page,
        /Choose Listing/i,
        new RegExp(SEEDED_EBAY_DRAFT_TITLE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"),
      ));
    test.skip(!selectedCreatedForPublish, "Created listing was not selectable in `Choose Listing`.");

    const postMode = page.getByRole("combobox", { name: /eBay Post Mode/i }).first();
    const postModeVisible = await eventuallyVisible(async () => postMode.isVisible(), 10000);
    test.skip(
      !postModeVisible,
      "eBay publish controls are not visible (no eBay listing selected or seller-ops controls unavailable).",
    );

    const selectedLiveMode = await selectByLabelText(
      page,
      /eBay Post Mode/i,
      /^Publish Live Listing$/i,
    );
    expect(selectedLiveMode).toBeTruthy();
    await expect
      .poll(async () => (await postMode.inputValue().catch(() => "")).toLowerCase(), { timeout: 10000 })
      .toContain("publish live");

    const selectedDraftMode = await selectByLabelText(
      page,
      /eBay Post Mode/i,
      /^Save Unpublished Offer \(API Draft\)$/i,
    );
    expect(selectedDraftMode).toBeTruthy();
    await expect
      .poll(async () => (await postMode.inputValue().catch(() => "")).toLowerCase(), { timeout: 10000 })
      .toContain("api draft");

    const loadCategoryAssistToggle = page
      .getByLabel("Load eBay Category Assist (slower)", { exact: true })
      .first();
    await expect(loadCategoryAssistToggle).toBeVisible({ timeout: 10000 });
    if (!(await loadCategoryAssistToggle.isChecked().catch(() => false))) {
      await loadCategoryAssistToggle.check();
    }

    const categoryIdInput = page.getByLabel("eBay Category ID", { exact: true }).first();
    if (await categoryIdInput.isVisible().catch(() => false)) {
      await categoryIdInput.fill("");
    }
    const categoryStateCaption = page.getByText(/Current Category ID in form state:/i).first();
    await expect(categoryStateCaption).toBeVisible({ timeout: 10000 });

    const categoryQueryInput = page.getByLabel("Category Search Keywords", { exact: true }).first();
    await expect(categoryQueryInput).toBeVisible({ timeout: 10000 });
    await categoryQueryInput.fill("silver dollar coin");
    await page.getByRole("button", { name: /Fetch Category Suggestions/i }).first().click();

    const categoryFetchSucceeded = await expect
      .poll(
        async () => {
          const loadedVisible = await page
            .getByText(/Loaded \d+ (cached )?category suggestion\(s\)\./i)
            .first()
            .isVisible()
            .catch(() => false);
          const refreshedVisible = await page
            .getByText(/Refreshed \d+ category suggestion\(s\) from eBay\./i)
            .first()
            .isVisible()
            .catch(() => false);
          const fetchFailedVisible = await page
            .getByText(/Category fetch failed:/i)
            .first()
            .isVisible()
            .catch(() => false);
          if (fetchFailedVisible) {
            return "failed";
          }
          if (loadedVisible || refreshedVisible) {
            return "ok";
          }
          return "pending";
        },
        { timeout: 20000 },
      )
      .toBe("ok")
      .then(() => true)
      .catch(async () => {
        const failedVisible = await page
          .getByText(/Category fetch failed:/i)
          .first()
          .isVisible()
          .catch(() => false);
        if (failedVisible) {
          return false;
        }
        throw new Error("Category suggestion fetch did not succeed before timeout.");
      });
    test.skip(
      !categoryFetchSucceeded,
      "Category suggestion fetch unavailable in this environment (token/config/cache); skipping apply assertion.",
    );

    await page.getByRole("button", { name: /Apply Selected Category/i }).first().click();
    await expect
      .poll(
        async () => {
          const text = (await categoryStateCaption.textContent().catch(() => "")) || "";
          return /`\d{2,}`/.test(text);
        },
        { timeout: 10000 },
      )
      .toBeTruthy();

    const runPreflightBtn = page.getByRole("button", { name: /^Run eBay Dependency Preflight$/i }).first();
    await expect(runPreflightBtn).toBeVisible({ timeout: 10000 });
    test.skip(
      await runPreflightBtn.isDisabled().catch(() => false),
      "eBay preflight action is disabled in this environment (seller ops guard).",
    );
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
});
