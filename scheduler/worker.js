// Starts the tracker workflow on Cloudflare's cron. GitHub's own hourly
// schedule ran about 5 times a day in Oct 2026, with gaps up to 9 hours.
const DISPATCH_URL =
  "https://api.github.com/repos/jaredrojas08/internship-tracker/actions/workflows/update_internships.yml/dispatches";

export default {
  async scheduled(event, env) {
    const response = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "internship-tracker-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    // Thrown so the failure shows in the Worker's logs, e.g. an expired token.
    if (!response.ok) {
      throw new Error(`GitHub dispatch failed: ${response.status} ${await response.text()}`);
    }
  },
};
