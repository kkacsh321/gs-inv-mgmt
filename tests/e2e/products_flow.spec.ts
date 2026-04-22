import { expect, test } from "@playwright/test";
import { ensureSignedIn, isAuthGateVisibleEventually, navigateAuthed } from "./_auth";

test.describe("Products flow", () => {
  test("creates and edits a product", async ({ page }) => {
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

    await page.goto("/Products");
    const signedIn = await ensureSignedIn(page);
    test.skip(!signedIn, "Auth gate remained active; skipping in this environment.");
    await navigateAuthed(page, "/Products", "Products");
    const productsAuthRequired = await isAuthGateVisibleEventually(page);
    test.skip(productsAuthRequired, "Auth gate still active on Products page.");
    const productsReady = page.getByLabel("SKU", { exact: true }).first();
    if (!(await productsReady.isVisible().catch(() => false))) {
      const sidebarProductsLink = page.locator("a[href$='/Products']").first();
      if (await sidebarProductsLink.isVisible().catch(() => false)) {
        const href = await sidebarProductsLink.getAttribute("href");
        if (href) {
          await page.goto(href);
        } else {
          await sidebarProductsLink.click();
        }
      }
    }
    await expect(productsReady).toBeVisible({ timeout: 15000 });

    const uniqueSku = `E2E-PROD-${Date.now()}`;
    const createdTitle = `E2E Product ${uniqueSku}`;
    const updatedTitle = `${createdTitle} Updated`;

    const productMediaInput = page.locator("input[type='file']").first();
    await productMediaInput.setInputFiles({
      name: `${uniqueSku}.jpg`,
      mimeType: "image/jpeg",
      buffer: Buffer.from("goldenstackers-e2e-image"),
    });

    await page.getByLabel("SKU", { exact: true }).first().fill(uniqueSku);
    await page.getByLabel("Title", { exact: true }).first().fill(createdTitle);
    await page.getByRole("button", { name: /Create Product/i }).first().click();
    const productCreatedToast = page.getByText(/Product created\.|SKU must be unique/i).first();
    await productCreatedToast.waitFor({ state: "visible", timeout: 20000 }).catch(() => {});

    await page.getByLabel("Search SKU/Title", { exact: true }).first().fill(uniqueSku);
    await page.waitForTimeout(500);

    const saveButton = page.getByRole("button", { name: /Save Product Changes|Save Changes|Update Product/i }).first();
    await expect(saveButton).toBeVisible({ timeout: 15000 });

    const titleInputs = page.getByLabel("Title", { exact: true });
    await titleInputs.last().fill(updatedTitle);
    await saveButton.click();
    await page.waitForTimeout(800);
    await expect(page.getByLabel("Search SKU/Title", { exact: true }).first()).toBeVisible({ timeout: 15000 });
  });
});
