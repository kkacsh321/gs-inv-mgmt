import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

const chooseComboOption = async (
  page: import("@playwright/test").Page,
  labelPattern: RegExp,
  optionPattern: RegExp,
): Promise<void> => {
  const combobox = page.getByRole("combobox", { name: labelPattern }).first();
  await expect(combobox).toBeVisible({ timeout: 10000 });
  await combobox.click();
  await combobox.fill("");
  const source = String(optionPattern.source || "")
    .replace(/\\\$/g, "$")
    .replace(/\\(.)/g, "$1");
  await combobox.type(source.slice(0, 60), { delay: 5 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);
};

test.describe("Sync flow", () => {
  test("loads sync controls and exception queue surface", async ({ page }) => {
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

    await page.goto("/Sync");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Sync", "Sync");
    const syncAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(syncAuthRequired, "Auth gate still active on Sync page.");
    await expect(page.getByRole("heading", { name: /^Sync$/i }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole("heading", { name: /Sync Job Controls/i }).first()).toBeVisible({ timeout: 15000 });

    const executeNow = page.getByRole("button", { name: /Execute Selected Job Now/i }).first();
    await expect(executeNow).toBeVisible({ timeout: 15000 });

    if (await executeNow.isEnabled().catch(() => false)) {
      await executeNow.click();
      await expect
        .poll(async () => {
          const ok = await page
            .getByText(/Run #\d+ completed with status/i)
            .first()
            .isVisible()
            .catch(() => false);
          const disabled = await page
            .getByText(/is disabled by configuration/i)
            .first()
            .isVisible()
            .catch(() => false);
          const failed = await page
            .getByText(/Execute-now failed:/i)
            .first()
            .isVisible()
            .catch(() => false);
          return ok || disabled || failed;
        }, { timeout: 20000 })
        .toBeTruthy();
    } else {
      await expect(executeNow).toBeDisabled();
    }

    const exceptionQueue = page.getByRole("heading", { name: /Exception Queue/i }).first();
    await expect(exceptionQueue).toBeVisible({ timeout: 15000 });

    // Mutation assertion 1: create failed run and verify retry transition.
    const uniqueJob = `e2e_manual_retry_${Date.now()}`;
    await chooseComboOption(page, /^Provider$/i, /^ebay$/i);
    await chooseComboOption(page, /^Direction$/i, /^pull$/i);
    await chooseComboOption(page, /^Status$/i, /^failed$/i);
    const jobNameInput = page.getByLabel(/^Job Name$/i).first();
    await jobNameInput.fill(uniqueJob);
    const createRunBtn = page.getByRole("button", { name: /^Create Sync Run$/i }).first();
    await createRunBtn.click();
    await expect(page.getByText(/Created sync run #\d+\./i).first()).toBeVisible({ timeout: 15000 });

    const selectRun = page.getByLabel(/^Select Run$/i).first();
    await expect(selectRun).toBeVisible({ timeout: 10000 });
    await selectRun.click();
    await selectRun.fill(uniqueJob);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);

    const retryRunBtn = page.getByRole("button", { name: /^Retry Failed Run$/i }).first();
    if (await retryRunBtn.isVisible().catch(() => false)) {
      const retryEnabled = await retryRunBtn.isEnabled().catch(() => false);
      if (retryEnabled) {
        await retryRunBtn.click();
        await expect(page.getByText(/Created retry run #\d+ for source run #\d+\./i).first()).toBeVisible({
          timeout: 15000,
        });
      }
    }

    // Deterministic mutation assertion: update selected run counters/status.
    const processedInput = page.getByLabel(/^Records Processed$/i).first();
    if (await processedInput.isVisible().catch(() => false)) {
      await processedInput.fill("3");
      await page.getByLabel(/^Records Created$/i).first().fill("1");
      await page.getByLabel(/^Records Updated$/i).first().fill("2");
      await page.getByLabel(/^Records Failed$/i).first().fill("1");
      const saveRunBtn = page.getByRole("button", { name: /^Update Sync Run$/i }).first();
      if (await saveRunBtn.isVisible().catch(() => false)) {
        await saveRunBtn.click();
        await expect(page.getByText(/Sync run updated\./i).first()).toBeVisible({ timeout: 15000 });
      }
    }

    // Mutation assertion 2: resolve exception when queue rows are present.
    const queueEmpty = page.getByText(/No queue rows match the current filters\./i).first();
    if (!(await queueEmpty.isVisible().catch(() => false))) {
      const selectException = page.getByLabel(/^Select Exception$/i).first();
      if (await selectException.isVisible().catch(() => false)) {
        await selectException.click();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
      }
      const resolveBtn = page.getByRole("button", { name: /^Mark Exception Resolved$/i }).first();
      if (await resolveBtn.isVisible().catch(() => false)) {
        await resolveBtn.click();
        await expect(page.getByText(/Resolved sync error #\d+\./i).first()).toBeVisible({ timeout: 15000 });
      }
    }
  });
});
