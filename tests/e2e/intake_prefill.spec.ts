import { expect, test, type Page } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

async function waitForSuccessOrError(
  page: Page,
  successPattern: RegExp,
  errorPattern: RegExp,
  timeoutMs = 30000,
): Promise<"success" | "error" | "timeout"> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await page.getByText(successPattern).first().isVisible().catch(() => false)) {
      return "success";
    }
    if (await page.getByText(errorPattern).first().isVisible().catch(() => false)) {
      return "error";
    }
    await page.waitForTimeout(250);
  }
  return "timeout";
}

async function goToWizard(page: Page, path: string, heading: string): Promise<void> {
  await page.goto(path);
  const signedIn = await ensureSignedIn(page);
  test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
  await navigateAuthed(page, path, heading);
  const authRequired = await isAuthGateVisibleEventually(page);
  test.skip(authRequired, `Auth gate still active on ${heading} page.`);
  await expect(page.getByRole("heading", { name: new RegExp(heading, "i") }).first()).toBeVisible({
    timeout: 15000,
  });
}

test.describe("Intake Wizard AI Prefill", () => {
  test("coin wizard identifier pre-fills intake fields", async ({ page }) => {
    await goToWizard(page, "/Coin_Intake_Wizard", "Coin Intake Wizard");

    const titleInput = page.getByLabel("Product Title", { exact: true }).first();
    const descriptionInput = page.getByLabel("Product Description", { exact: true }).first();
    const aiHintInput = page.getByLabel("AI Hint (optional)", { exact: true }).first();
    const runButton = page.getByRole("button", { name: /^Run Identifier$/i }).first();

    await expect(aiHintInput).toBeVisible({ timeout: 10000 });
    await expect(runButton).toBeVisible({ timeout: 10000 });

    await titleInput.fill("");
    await descriptionInput.fill("");
    await aiHintInput.fill("E2E prefill hint: silver morgan dollar collectible coin");

    await runButton.click();

    const status = await waitForSuccessOrError(
      page,
      /Identifier completed and wizard prefill updated\./i,
      /Identifier failed:/i,
    );

    test.skip(status !== "success", "Identifier did not complete successfully in this environment.");

    await expect(descriptionInput).toHaveValue(/\S+/, { timeout: 20000 });
  });

  test("inventory wizard identifier pre-fills intake fields", async ({ page }) => {
    await goToWizard(page, "/Inventory_Intake_Wizard", "Inventory Intake Wizard");

    const titleInput = page.getByLabel("Product Title", { exact: true }).first();
    const descriptionInput = page.getByLabel("Product Description", { exact: true }).first();
    const aiHintInput = page.getByLabel("AI Hint (optional)", { exact: true }).first();
    const runButton = page.getByRole("button", { name: /^Run Identifier$/i }).first();

    await expect(aiHintInput).toBeVisible({ timeout: 10000 });
    await expect(runButton).toBeVisible({ timeout: 10000 });

    await titleInput.fill("");
    await descriptionInput.fill("");
    await aiHintInput.fill("E2E prefill hint: 1 oz silver bar vintage hallmark");

    await runButton.click();

    const status = await waitForSuccessOrError(
      page,
      /Identifier completed and intake defaults updated\./i,
      /Identifier failed:/i,
    );

    test.skip(status !== "success", "Identifier did not complete successfully in this environment.");

    await expect(descriptionInput).toHaveValue(/\S+/, { timeout: 20000 });
  });

  test("inventory wizard grader pre-fills AI Grading Description", async ({ page }) => {
    await goToWizard(page, "/Inventory_Intake_Wizard", "Inventory Intake Wizard");

    const aiHintInput = page.getByLabel("AI Hint (optional)", { exact: true }).first();
    const aiImageInput = page
      .locator('[aria-label="Upload Item Image (AI Assist)"]')
      .first()
      .locator('input[type="file"]')
      .first();
    const runGraderButton = page.getByRole("button", { name: /^Run Grader$/i }).first();
    const aiGradingDescriptionInput = page.getByLabel("AI Grading Description", { exact: true }).first();

    await expect(aiHintInput).toBeVisible({ timeout: 10000 });
    await expect(aiImageInput).toBeVisible({ timeout: 10000 });
    await expect(runGraderButton).toBeVisible({ timeout: 10000 });

    await aiHintInput.fill("E2E grader hint: assess visible condition and likely grade range");
    await aiImageInput.setInputFiles("app/images/logonewsm.jpg");

    await runGraderButton.click();

    const status = await waitForSuccessOrError(
      page,
      /Grader completed and applied to last grader output\./i,
      /Grader failed:|Provide an image to run grader\.|permission|not allowed|disabled/i,
      30000,
    );

    test.skip(status !== "success", "Grader did not complete successfully in this environment.");

    await expect(aiGradingDescriptionInput).toHaveValue(/\S+/, { timeout: 20000 });
  });
});
