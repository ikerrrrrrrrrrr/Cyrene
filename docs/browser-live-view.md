# Browser Live View & Login Takeover

When the agent uses the browser, the chat UI shows a **live view** of the page —
what the agent sees and the actions it takes — in the right-hand panel. When the
agent hits a login wall, CAPTCHA, or 2FA, it can **hand the browser to you**: a real
window opens, you log in, and the agent resumes in the same (now authenticated)
session.

## Setup

The browser tools work without any extra dependency — they fall back to a plain
HTTP fetch (`httpx`) for `browser_navigate`. For the full experience (real browser
automation, live screencast, and login takeover), install Playwright:

```bash
pip install -e ".[browser]"   # installs the optional "browser" extra (Playwright)
playwright install chromium    # one-time download of the Chromium runtime
```

If Playwright (or the Chromium runtime) is missing, the live-view panel shows a
hint and `browser_navigate` degrades to a text-only fetch; `browser_click` /
`browser_type` / `browser_request_takeover` report that Playwright is required.

## How the live view works

- A single **persistent browser context** is launched lazily and reused across all
  browser actions. Its profile lives on disk at `<DATA_DIR>/browser_profile`, so
  cookies / logins **survive across runs** — once you log into a site, the agent
  stays logged in next time.
- After each action (`navigate` / `click` / `type`) the backend publishes a
  `browser_frame` SSE event (a JPEG snapshot + the action + a target box) so the
  panel can show the latest state and an action ribbon.
- A continuous **CDP screencast** streams live JPEG frames over the
  `GET /ws/browser` WebSocket to a `<canvas>` in the panel.
- The panel auto-reveals in the chat's right sidebar the moment the agent starts
  browsing, and a **Browser** tab lets you reopen it.

## Login takeover (native window)

1. The agent calls the `browser_request_takeover` tool the moment it hits a login
   wall (it is prompted to do this early, before deep work on the page).
2. The shared browser **restarts headed** on the same profile, a real Chromium
   window comes to the foreground, and the agent **pauses** (reusing the standard
   "awaiting user" mechanism). The in-chat screencast pauses and the panel shows a
   takeover card — your login pixels are not streamed.
3. You complete the login in the native window and click **「我已完成登录」**.
4. The browser **restarts headless** on the same profile (now authenticated) and
   the agent resumes the round automatically.

Because the profile is persistent, the window usually only needs to appear once
per site.

> **Local only.** Native-window takeover assumes the UI and the browser run on the
> same machine (true for the desktop / local web app). The persistent profile means
> takeover is rarely needed after the first login.

## Configuration

These are read from the config store (env keys); defaults shown:

| Key | Default | Meaning |
|-----|---------|---------|
| `CYRENE_BROWSER_HEADLESS` | `1` | Normal mode. Set to `0`/`false` to always run headed. The takeover flow restarts headed regardless. |
| `CYRENE_BROWSER_SCREENCAST_QUALITY` | `60` | JPEG quality (1–100) for live frames. |
| `CYRENE_BROWSER_WIDTH` | `1280` | Viewport / screencast width. |
| `CYRENE_BROWSER_HEIGHT` | `800` | Viewport / screencast height. |

The profile directory is `<DATA_DIR>/browser_profile`.

## Permissions

Browser navigation is a **network read**, like `WebFetch` / `WebSearch`, so it does
**not** require workspace scope elevation. The only on-disk writes are the browser
profile, which lives inside `DATA_DIR` (in-workspace). Login takeover is an
explicit, user-driven action (you perform the login in the native window).

## Tools

| Tool | Purpose |
|------|---------|
| `browser_navigate` | Open a URL in the shared session; return readable text. |
| `browser_screenshot` | Screenshot the current page to a temp PNG. |
| `browser_click` | Click an element by CSS selector. |
| `browser_type` | Type into an input (optionally submit). |
| `browser_request_takeover` | Open a real window for the user to log in, pause, then resume authenticated. |
