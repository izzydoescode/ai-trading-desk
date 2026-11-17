import { defineConfig, devices } from "@playwright/test";

const PORT = 8137;
const BASE_URL = `http://127.0.0.1:${PORT}`;

/** Browser tests against the real desk server serving the built terminal. The server
 *  replays the bundled synthetic fixture, so nothing here touches the network. */
export default defineConfig({
  testDir: "./e2e",
  // One desk, one replay at a time: the tests share the server and run in order.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 45_000,
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 1000 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
  webServer: {
    command: `uv run trading-desk desk serve --port ${PORT} --speed 16 --ui-dir web/dist`,
    cwd: "..",
    url: `${BASE_URL}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
