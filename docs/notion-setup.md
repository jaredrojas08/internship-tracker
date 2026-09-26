# Notion setup

One-time, about five minutes. Do steps 1-5 now; step 6 waits until the code exists.

## 1. Create the connection

Go to <https://www.notion.so/my-integrations>. Notion calls these
**Connections** now, under Developer tools; older docs say "integration".

Click **New connection**.

- Connection name: `internship-radar`
- Authentication method: **API token**

**API token, not OAuth.** API token is workspace-scoped and static, which is
what an unattended cron job needs. OAuth is user-scoped and expects a browser
consent flow that a GitHub Actions run cannot complete.

Create it, then copy the token. It starts with `ntn_`.

## 2. Create the page the database will live on

In Notion, make a new empty page called **Internships**. Leave it blank. The
database gets created as a child of this page.

## 3. Connect the integration to that page

On that page: the `...` menu, top right, then **Connections**, then
**Connect to**, then pick `internship-radar`.

**This is the step that gets skipped.** An integration can only see pages that
have been explicitly shared with it, so without this the API returns 404 on a
page you are looking at right now.

## 4. Copy the page id

From the page's URL, take the 32 characters at the end of the path, before any
`?`:

```
notion.so/Internships-2b4f8a1c9d3e4f5a8b7c6d5e4f3a2b1c?pvs=4
                      \________ this, 32 chars ________/
```

## 5. Store the two values

Never paste these into a chat.

`.env` in this repo (already gitignored):

```
NOTION_TOKEN=ntn_...
NOTION_PARENT_PAGE_ID=2b4f8a1c9d3e4f5a8b7c6d5e4f3a2b1c
```

GitHub: repo **Settings**, **Secrets and variables**, **Actions**,
**New repository secret**. Same two names, same two values.

## 6. Later: the database id

Once the Notion sink is wired up, run:

```bash
./venv/bin/python internship_tracker.py --create-database
```

It prints a database id. Add that as a third repository secret,
`NOTION_DATABASE_ID`, and to `.env`.

## Checking it worked

A 404 on a page that exists means step 3 was missed. A 401 means the token is
wrong or truncated.
