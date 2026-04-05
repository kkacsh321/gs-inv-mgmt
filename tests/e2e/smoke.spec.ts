import { expect, test } from "@playwright/test";

test.describe("Streamlit smoke", () => {
  test("loads app shell", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/GoldenStackers Inventory/i);

    const appTitle = page.getByText(/GoldenStackers Inventory/i).first();
    const signinRequired = page.getByText(/Sign in required/i).first();
    const sessionIdentity = page.getByText(/Session Identity/i).first();

    // App can render either authenticated home content or auth-gated shell.
    await expect
      .poll(
        async () =>
          (await appTitle.isVisible()) ||
          (await signinRequired.isVisible()) ||
          (await sessionIdentity.isVisible()),
        { timeout: 10000 },
      )
      .toBeTruthy();
  });
});
