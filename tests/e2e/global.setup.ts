import { execSync } from "node:child_process";

export default async function globalSetup() {
  const shouldSeed = (process.env.PLAYWRIGHT_SEED ?? "1").trim().toLowerCase();
  if (["0", "false", "no", "off"].includes(shouldSeed)) {
    return;
  }

  const env = {
    ...process.env,
    E2E_USERNAME: process.env.E2E_USERNAME || "e2e",
    E2E_PASSWORD: process.env.E2E_PASSWORD || "e2e-password-123",
    E2E_ROLE: process.env.E2E_ROLE || "admin",
    E2E_ENSURE_ROLE_PERMISSIONS: process.env.E2E_ENSURE_ROLE_PERMISSIONS || "true",
  };

  type SeedAttempt = { ok: boolean; output: string };
  const trySeed = (cmd: string, quiet = true): SeedAttempt => {
    try {
      if (quiet) {
        execSync(cmd, { stdio: "pipe", env, encoding: "utf-8" });
      } else {
        execSync(cmd, { stdio: "inherit", env, encoding: "utf-8" });
      }
      return { ok: true, output: "" };
    } catch (error) {
      const message =
        typeof error === "object" && error
          ? String(
              (error as { stdout?: string; stderr?: string; message?: string }).stderr ||
                (error as { stdout?: string; stderr?: string; message?: string }).stdout ||
                (error as { stdout?: string; stderr?: string; message?: string }).message ||
                ""
            )
          : "";
      return { ok: false, output: message.trim() };
    }
  };

  const customSeedCmd = (process.env.PLAYWRIGHT_SEED_CMD || "").trim();
  if (customSeedCmd) {
    if (!trySeed(customSeedCmd, false).ok) {
      throw new Error(`PLAYWRIGHT_SEED_CMD failed: ${customSeedCmd}`);
    }
    return;
  }

  const localSeed = trySeed("python -m app.db.seed", true);
  if (localSeed.ok) {
    return;
  }
  const dockerSeed = trySeed("docker compose exec -T app python -m app.db.seed", true);
  if (dockerSeed.ok) {
    return;
  }

  const requireSeed = (process.env.PLAYWRIGHT_REQUIRE_SEED || (process.env.CI ? "1" : "0")).trim().toLowerCase();
  const shouldRequireSeed = ["1", "true", "yes", "on"].includes(requireSeed);
  const details = [localSeed.output, dockerSeed.output]
    .map((s) => s.trim())
    .filter(Boolean)
    .join("\n\n---\n\n");
  const message =
    "Unable to seed E2E data. Tried `python -m app.db.seed` and `docker compose exec -T app python -m app.db.seed`." +
    (details ? `\n\nSeed errors:\n${details}` : "");
  if (shouldRequireSeed) {
    throw new Error(message);
  }
  // Local/dev fallback: allow tests to continue when sandbox restrictions block seed.
  // This keeps e2e debugging usable in restricted environments while CI remains strict.
  // eslint-disable-next-line no-console
  console.warn(`[playwright globalSetup] ${message}\nProceeding without seed (PLAYWRIGHT_REQUIRE_SEED=0).`);
}
