import { defineConfig } from "@playwright/test";

// Browser tests for the two screens. They start their own API (on its own database, with
// the keyword stand-in as the model) and their own build of this app, so they need
// nothing but Docker's Postgres to be running.
//
//   npm run e2e
const API = "http://localhost:8010";
const WEB = "http://localhost:3010";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: WEB,
    // The Chrome already installed on this machine. No browser download.
    channel: "chrome",
    headless: true,
    viewport: { width: 1280, height: 860 },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "uv run python ../scripts/e2e_api.py",
      url: `${API}/healthz`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { RD_E2E_WEB_ORIGIN: WEB },
    },
    {
      command: "npm run build && npm run start -- -p 3010",
      url: WEB,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        NEXT_PUBLIC_API_URL: API,
        // Force the local demo sign-in, whatever the shell has set.
        NEXT_PUBLIC_SUPABASE_URL: "",
      },
    },
  ],
});
