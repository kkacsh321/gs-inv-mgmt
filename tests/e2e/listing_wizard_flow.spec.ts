import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

test.describe("Listing Wizard flow", () => {
  const hasWorkflowSchemaError = async (page: import("@playwright/test").Page): Promise<boolean> => {
    const err = page
      .getByText(/relation \"workflow_drafts\" does not exist|sqlalchemy\.exc\.ProgrammingError/i)
      .first();
    return err.isVisible().catch(() => false);
  };

  test("supports workflow draft save/resume/clear controls", async ({ page }) => {
    await page.goto("/Listing_Wizard");
    let signedIn = false;
    try {
      signedIn = await ensureSignedIn(page);
    } catch {
      test.skip(true, "Sign-in controls unavailable in this environment.");
    }
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");

    await navigateAuthed(page, "/Listing_Wizard", "Listing Wizard");
    const authRequired = await isAuthGateVisibleEventually(page);
    test.skip(authRequired, "Auth gate still active on Listing Wizard page.");
    test.skip(
      await hasWorkflowSchemaError(page),
      "workflow_drafts migration not applied in this environment; skipping draft-state assertions.",
    );
    await expect(page.getByRole("heading", { name: /^Listing Wizard$/i }).first()).toBeVisible({
      timeout: 15000,
    });

    const noProductsHint = page.getByText(/Create at least one product before using Listing Wizard\./i).first();
    test.skip(await noProductsHint.isVisible().catch(() => false), "No products available for Listing Wizard test.");

    const saveBtn = page.getByRole("button", { name: /^Save Workflow Draft$/i }).first();
    const resumeBtn = page.getByRole("button", { name: /^Resume Saved Draft$/i }).first();
    const clearBtn = page.getByRole("button", { name: /^(Clear Workflow Draft|Reset Saved Draft)$/i }).first();

    await expect(saveBtn).toBeVisible({ timeout: 10000 });
    await expect(resumeBtn).toBeVisible({ timeout: 10000 });
    await expect(clearBtn).toBeVisible({ timeout: 10000 });

    await saveBtn.click();
    await expect
      .poll(
        async () => {
          const savedToast = await page.getByText(/Workflow draft saved/i).first().isVisible().catch(() => false);
          const savedCaption = await page.getByText(/Saved draft available/i).first().isVisible().catch(() => false);
          return savedToast || savedCaption;
        },
        { timeout: 15000 },
      )
      .toBeTruthy();

    await resumeBtn.click();
    await expect
      .poll(
        async () => page.getByText(/Resumed saved workflow draft/i).first().isVisible().catch(() => false),
        { timeout: 15000 },
      )
      .toBeTruthy();

    await clearBtn.click();
    await expect
      .poll(
        async () => page.getByText(/Cleared saved workflow draft/i).first().isVisible().catch(() => false),
        { timeout: 15000 },
      )
      .toBeTruthy();
  });

  test("supports category source, fixed/store quantity, and HTML preview mode", async ({ page }) => {
    await page.goto("/Listings");
    let signedIn = false;
    try {
      signedIn = await ensureSignedIn(page);
    } catch {
      test.skip(true, "Sign-in controls unavailable in this environment.");
    }
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");

    await navigateAuthed(page, "/Listing_Wizard", "Listing Wizard");
    const authRequired = await isAuthGateVisibleEventually(page);
    test.skip(authRequired, "Auth gate still active on Listing Wizard page.");
    test.skip(
      await hasWorkflowSchemaError(page),
      "workflow_drafts migration not applied in this environment; skipping wizard UI assertions.",
    );

    await expect(page.getByRole("heading", { name: /^Listing Wizard$/i }).first()).toBeVisible({
      timeout: 15000,
    });
    const noProductsHint = page.getByText(/Create at least one product before using Listing Wizard\./i).first();
    test.skip(await noProductsHint.isVisible().catch(() => false), "No products available for Listing Wizard test.");

    const productSelect = page.getByRole("combobox", { name: /Product/i }).first();
    await expect(productSelect).toBeVisible({ timeout: 10000 });

    await page.getByRole("combobox", { name: /Listing Mode/i }).first().click();
    await page.getByText(/Store Listing \(30 days\)/i).first().click();
    await expect(page.getByLabel("Quantity to List", { exact: true })).toBeVisible({ timeout: 10000 });
    await page.getByLabel("Quantity to List", { exact: true }).fill("2");
    await expect(page.getByLabel("Quantity to List", { exact: true })).toHaveValue("2");

    await expect(page.getByRole("combobox", { name: /Category Source/i }).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByLabel("eBay Category ID", { exact: true })).toBeVisible({ timeout: 10000 });
    await page.getByLabel("eBay Category ID", { exact: true }).fill("111111");
    await expect(page.getByLabel("eBay Category ID", { exact: true })).toHaveValue("111111");

    const uniqueTitle = `E2E Wizard ${Date.now()}`;
    await page.getByLabel("Listing Title", { exact: true }).fill(uniqueTitle);
    await page.getByLabel("Listing Details", { exact: true }).fill("<p><strong>E2E HTML Preview</strong></p>");

    const previewToggle = page.locator("summary:has-text('Preview Listing Draft')").first();
    await previewToggle.click({ force: true });
    const previewMode = page.getByRole("combobox", { name: /Details Preview Mode/i }).first();
    const previewModeVisible = await previewMode.isVisible().catch(() => false);
    if (previewModeVisible) {
      await previewMode.click();
      await page.getByText(/^Raw Source$/i).first().click();
      await expect(page.getByText(/E2E HTML Preview/i).first()).toBeVisible({ timeout: 10000 });

      await previewMode.click();
      await page.getByText(/^Rendered HTML$/i).first().click();
    } else {
      await expect(page.getByText(/Preview Listing Draft/i).first()).toBeVisible({ timeout: 10000 });
    }
    await expect(page.getByText(/Create Draft Listing/i).first()).toBeVisible({ timeout: 10000 });
  });

  test("supports direct-post mode toggle and eBay dependency preflight card flow", async ({ page }) => {
    await page.goto("/Listing_Wizard");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");

    await navigateAuthed(page, "/Listing_Wizard", "Listing Wizard");
    const authRequired = await isAuthGateVisibleEventually(page);
    test.skip(authRequired, "Auth gate still active on Listing Wizard page.");
    test.skip(
      await hasWorkflowSchemaError(page),
      "workflow_drafts migration not applied in this environment; skipping wizard UI assertions.",
    );
    await expect(page.getByRole("heading", { name: /^Listing Wizard$/i }).first()).toBeVisible({
      timeout: 15000,
    });

    const noProductsHint = page.getByText(/Create at least one product before using Listing Wizard\./i).first();
    test.skip(await noProductsHint.isVisible().catch(() => false), "No products available for Listing Wizard test.");

    const postNowLabel = page
      .locator("label", { hasText: /Post to eBay Immediately \(single listing, non-batch\)/i })
      .first();
    await postNowLabel.scrollIntoViewIfNeeded();
    await expect(postNowLabel).toBeVisible({ timeout: 10000 });
    await postNowLabel.click();

    const directMode = page.getByRole("combobox", { name: /Direct eBay Post Mode/i }).first();
    await expect(directMode).toBeVisible({ timeout: 10000 });
    await expect(directMode).toBeEnabled({ timeout: 10000 });
    await directMode.click();
    await expect(page.getByText(/Publish Live Listing/i).first()).toBeVisible({ timeout: 10000 });
    await page.getByText(/Save Unpublished Offer \(API Draft\)/i).first().click();

    const runPreflightBtn = page.getByRole("button", { name: /^Run eBay Dependency Preflight$/i }).first();
    await runPreflightBtn.scrollIntoViewIfNeeded();
    await expect(runPreflightBtn).toBeVisible({ timeout: 10000 });
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

  test("handles direct-post create feedback and keeps post-create navigation links visible", async ({ page }) => {
    await page.goto("/Listing_Wizard");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");

    await navigateAuthed(page, "/Listing_Wizard", "Listing Wizard");
    const authRequired = await isAuthGateVisibleEventually(page);
    test.skip(authRequired, "Auth gate still active on Listing Wizard page.");
    test.skip(
      await hasWorkflowSchemaError(page),
      "workflow_drafts migration not applied in this environment; skipping wizard UI assertions.",
    );
    await expect(page.getByRole("heading", { name: /^Listing Wizard$/i }).first()).toBeVisible({
      timeout: 15000,
    });

    const noProductsHint = page.getByText(/Create at least one product before using Listing Wizard\./i).first();
    test.skip(await noProductsHint.isVisible().catch(() => false), "No products available for Listing Wizard test.");

    const listingTitle = `E2E Wizard Direct Post ${Date.now()}`;
    await page.getByLabel("Listing Title", { exact: true }).fill(listingTitle);
    await page.getByLabel("Listing Details", { exact: true }).fill("E2E direct-post feedback coverage listing.");
    await page.getByLabel("eBay Category ID", { exact: true }).fill("16679");

    const maybeFill = async (label: string, value: string) => {
      const field = page.getByLabel(label, { exact: true }).first();
      if (await field.isVisible().catch(() => false)) {
        await field.fill(value);
      }
    };
    await maybeFill("Merchant Location Key", "goldenstackers-main");
    await maybeFill("Payment Policy ID", "270030909020");
    await maybeFill("Fulfillment Policy ID", "270030910020");
    await maybeFill("Return Policy ID", "270030921020");
    await maybeFill("Buy It Now Price", "12.00");

    await page.getByRole("checkbox", { name: /Stay on Wizard after Create/i }).first().check();
    await page
      .getByRole("checkbox", { name: /Post to eBay Immediately \(single listing, non-batch\)/i })
      .first()
      .check();
    await maybeFill("eBay User Access Token", "");

    const createBtn = page.getByRole("button", { name: /^Create Draft Listing$/i }).first();
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    test.skip(
      await createBtn.isDisabled().catch(() => true),
      "Create Draft Listing is disabled in this environment (likely missing readiness prerequisites such as media/policies).",
    );
    await createBtn.click();

    await expect(page.getByText(/Created listing draft #/i).first()).toBeVisible({ timeout: 30000 });
    await expect
      .poll(
        async () => {
          const skipped = await page
            .getByText(/Direct eBay post skipped: missing user access token\./i)
            .first()
            .isVisible()
            .catch(() => false);
          const failed = await page.getByText(/Direct eBay post failed:/i).first().isVisible().catch(() => false);
          return skipped || failed;
        },
        { timeout: 20000 },
      )
      .toBeTruthy();
    await expect(page.getByText(/Last draft created from wizard:/i).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole("link", { name: /^Open Listings$/i }).first()).toBeVisible({ timeout: 15000 });
  });
});
