import { expect, Locator, Page } from "@playwright/test";

export function withCurrentAuth(page: Page, path: string): string {
  const token = new URL(page.url()).searchParams.get("auth");
  return token ? `${path}?auth=${encodeURIComponent(token)}` : path;
}

async function _extractAuthToken(page: Page): Promise<string | null> {
  const fromUrl = new URL(page.url()).searchParams.get("auth");
  if (fromUrl) {
    return fromUrl;
  }
  const tokenFromLink = await page.evaluate(() => {
    const links = Array.from(document.querySelectorAll("a[href]")) as HTMLAnchorElement[];
    for (const link of links) {
      try {
        const parsed = new URL(link.href, window.location.origin);
        const token = parsed.searchParams.get("auth");
        if (token) {
          return token;
        }
      } catch {
        // Ignore malformed href values.
      }
    }
    return null;
  });
  return tokenFromLink;
}

async function _anyVisible(locator: Locator): Promise<boolean> {
  const count = await locator.count();
  for (let i = 0; i < count; i += 1) {
    if (await locator.nth(i).isVisible().catch(() => false)) {
      return true;
    }
  }
  return false;
}

export async function isAuthGateVisible(page: Page): Promise<boolean> {
  const gateA = page.getByText(/^Sign in required\.$/i);
  const gateB = page.getByText(/Sign in required to access app pages/i);
  const gateC = page.getByRole("button", { name: /Sign In/i });
  return (await _anyVisible(gateA)) || (await _anyVisible(gateB)) || (await _anyVisible(gateC));
}

export async function isAuthGateVisibleEventually(page: Page, timeoutMs = 4000): Promise<boolean> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await isAuthGateVisible(page)) {
      return true;
    }
    await page.waitForTimeout(150);
  }
  return isAuthGateVisible(page);
}

export async function navigateAuthed(
  page: Page,
  path: string,
  linkLabel: string,
): Promise<void> {
  const token = await _extractAuthToken(page);
  if (token) {
    await page.goto(`${path}?auth=${encodeURIComponent(token)}`);
    return;
  }

  const viewMoreButton = page.getByRole("button", { name: /View \d+ more/i }).first();
  if (await viewMoreButton.isVisible().catch(() => false)) {
    await viewMoreButton.click().catch(() => {});
    await page.waitForTimeout(150);
  }

  const sidebarLink = page.getByRole("link", { name: new RegExp(`^${linkLabel}$`, "i") }).first();
  if (await sidebarLink.isVisible().catch(() => false)) {
    await sidebarLink.click({ timeout: 2500 }).catch(async () => {
      const href = await sidebarLink.getAttribute("href").catch(() => null);
      if (href) {
        await page.goto(href);
      } else {
        await page.goto(path);
      }
    });
    return;
  }
  await page.goto(path);
}

type SignInOptions = {
  username?: string;
  password?: string;
};

export async function ensureSignedIn(page: Page, options?: SignInOptions): Promise<boolean> {
  const username = options?.username || process.env.E2E_USERNAME || "e2e";
  const password = options?.password || process.env.E2E_PASSWORD || "e2e-password-123";
  if (!username || !password) {
    throw new Error("Missing E2E_USERNAME/E2E_PASSWORD.");
  }

  const signOutButton = page.getByRole("button", { name: /Sign Out/i }).first();
  const alreadySignedIn =
    (await signOutButton.isVisible().catch(() => false)) ||
    (await page.getByText(/Signed in as/i).first().isVisible().catch(() => false));
  const authRequiredNow = await isAuthGateVisible(page);
  if (alreadySignedIn && !authRequiredNow) {
    return true;
  }

  const sessionIdentityToggle = page.getByText(/Session Identity/i).first();
  if (await sessionIdentityToggle.isVisible().catch(() => false)) {
    await sessionIdentityToggle.click().catch(() => {});
  }

  const firstVisible = async (locator: Locator): Promise<Locator | null> => {
    const count = await locator.count();
    for (let i = 0; i < count; i += 1) {
      const candidate = locator.nth(i);
      if (await candidate.isVisible().catch(() => false)) {
        return candidate;
      }
    }
    return null;
  };

  const usernameInput =
    (await firstVisible(page.getByLabel("Username", { exact: true }))) ||
    page.getByLabel("Username", { exact: true }).first();
  const passwordInput =
    (await firstVisible(page.getByLabel("Password", { exact: true }))) ||
    page.getByLabel("Password", { exact: true }).first();
  const signInButton =
    (await firstVisible(page.getByRole("button", { name: /Sign In/i }))) ||
    page.getByRole("button", { name: /Sign In/i }).first();

  await expect(usernameInput).toBeVisible({ timeout: 6000 });
  await expect(passwordInput).toBeVisible({ timeout: 6000 });
  await expect(signInButton).toBeVisible({ timeout: 6000 });

  await usernameInput.fill(username);
  await passwordInput.fill(password);
  await expect(passwordInput).toHaveValue(password);

  const rememberCheckbox = page.getByLabel("Remember me on this browser").first();
  const rememberVisible = await rememberCheckbox.isVisible().catch(() => false);
  if (rememberVisible) {
    await rememberCheckbox.check();
  } else {
    await page.getByText("Remember me on this browser").first().click({ force: true });
  }

  await signInButton.scrollIntoViewIfNeeded().catch(() => {});
  try {
    await signInButton.click({ force: true });
  } catch {
    await passwordInput.press("Enter");
  }
  await page.waitForTimeout(250);

  const invalidLogin = page.getByText(/Invalid username\/password/i).first();
  if (await invalidLogin.isVisible().catch(() => false)) {
    throw new Error("E2E auth credentials rejected after seed.");
  }

  for (let i = 0; i < 30; i += 1) {
    const signOutVisible = await signOutButton.isVisible().catch(() => false);
    const signedInVisible = await page.getByText(/Signed in as/i).first().isVisible().catch(() => false);
    const authRequired = await isAuthGateVisible(page);
    if ((signOutVisible || signedInVisible) && !authRequired) {
      return true;
    }
    await page.waitForTimeout(250);
  }
  return false;
}

export async function ensureSignedInAs(
  page: Page,
  username: string,
  password: string,
): Promise<boolean> {
  const signedInBanner = page.getByText(/Signed in as/i).first();
  const signOutButton = page.getByRole("button", { name: /Sign Out/i }).first();

  const currentBannerText = ((await signedInBanner.textContent().catch(() => "")) || "").toLowerCase();
  const alreadyTargetUser =
    currentBannerText.includes(username.toLowerCase()) &&
    !(await isAuthGateVisible(page));
  if (alreadyTargetUser) {
    return true;
  }

  if (await signOutButton.isVisible().catch(() => false)) {
    await signOutButton.click().catch(() => {});
    await page.waitForTimeout(250);
  }

  return ensureSignedIn(page, { username, password });
}
