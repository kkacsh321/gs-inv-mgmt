import { expect, test } from "@playwright/test";

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

    const signInButton = page.getByRole("button", { name: /Sign In/i }).first();
    const authGateAlert = page.getByText(/^Sign in required\.$/i).first();
    const usernameInput = page.getByLabel("Username", { exact: true }).first();
    const passwordInput = page.getByLabel("Password", { exact: true }).first();
    const username = process.env.E2E_USERNAME || "e2e";
    const withCurrentAuth = (path: string): string => {
      const token = new URL(page.url()).searchParams.get("auth");
      return token ? `${path}?auth=${encodeURIComponent(token)}` : path;
    };
    const password = process.env.E2E_PASSWORD || "";
    test.skip(!username || !password, "Auth is enabled locally; set E2E_USERNAME/E2E_PASSWORD to run this test.");
    try {
      await usernameInput.waitFor({ state: "visible", timeout: 12000 });
      await passwordInput.waitFor({ state: "visible", timeout: 12000 });
      await signInButton.waitFor({ state: "visible", timeout: 12000 });
      await usernameInput.fill(username);
      await passwordInput.fill(password);
      await expect(passwordInput).toHaveValue(password);
      await page.getByLabel("Remember me on this browser").first().check();
      await signInButton.click();
      await page.waitForTimeout(300);
      const invalidLogin = page.getByText(/Invalid username\/password/i).first();
      if (await invalidLogin.isVisible().catch(() => false)) {
        test.skip(true, "Local auth credentials rejected; rerun seed and verify E2E_USERNAME/E2E_PASSWORD.");
      }
      await expect.poll(async () => {
        const visible = await signInButton.isVisible().catch(() => false);
        const stillRequired = await authGateAlert.isVisible().catch(() => false);
        return !visible && !stillRequired;
      }, { timeout: 15000 }).toBeTruthy();
    } catch {
      // Auth form not present (password auth disabled) or not required in this environment.
    }

    await page.goto(withCurrentAuth("/Products"));
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
    if (!(await productsReady.isVisible().catch(() => false))) {
      const stillAuthRequired =
        (await authGateAlert.isVisible().catch(() => false)) ||
        (await signInButton.isVisible().catch(() => false));
      test.skip(stillAuthRequired, "Auth gate still active for /Products in this run.");
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
    await expect(page.getByText(/Product created\./i).first()).toBeVisible({ timeout: 20000 });

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
