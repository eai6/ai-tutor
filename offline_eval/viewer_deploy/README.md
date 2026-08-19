# Multi-turn tutoring experiment viewer — Vercel deploy

`index.html` is a single self-contained static page (the experiment viewer:
transcripts + judge scoring + cross-cycle compare, plus manual grading and a
human-vs-judge comparison). It has **no build step and no backend** — Vercel just
serves the file.

## Manual grades are per-browser, and only a file makes them portable

The Grade tab scores a session against the same eight pedagogical dimensions the
teacher dashboard uses at `/dashboard/benchmark/sessions/`, and the Agreement tab
compares those verdicts with the judge's. Those grades live in **localStorage**,
which means:

- they are private to one browser profile on one machine — nothing is uploaded,
  and two people opening the same deployed URL do **not** see each other's work;
- clearing browser data deletes them;
- **Export grades** writes a JSON file, and **Import** merges one back (newer
  timestamp wins per session). That file is the only durable copy — commit it, or
  send it to whoever is collating.

The header shows how many sessions have grades that are not in an exported file
yet.

## Deploy (CLI — simplest)

From this directory:

```bash
npm i -g vercel        # once, if you don't have it
cd offline_eval/viewer_deploy
vercel                 # preview deploy → gives a URL
vercel --prod          # promote to the production URL
```

The first run asks a couple of setup questions (scope, project name) and links a
Vercel project; after that `vercel --prod` re-deploys.

Alternative: drag-and-drop this folder onto https://vercel.com/new (no CLI).

## ⚠️ Access — read before sharing the URL

**A Vercel deploy is PUBLIC by default.** This page embeds every session's full
chat transcript and the judge's scoring inline. Anyone with the URL can read it.
`X-Robots-Tag: noindex` (set in `vercel.json`) keeps it out of search engines, but
that is **not** access control. If this data shouldn't be public, turn on one of:

- **Vercel Authentication** (Project → Settings → Deployment Protection → Vercel
  Authentication) — restricts to your Vercel team. Free.
- **Password Protection** (same settings page) — a shared password. Pro plan.

Do this before circulating the link.

## Updating after a new eval cycle

The page is generated from the committed result JSONs:

```bash
python3 offline_eval/build_viewer.py   # regenerates offline_eval/viewer_deploy/index.html
cd offline_eval/viewer_deploy && vercel --prod
```

`build_viewer.py` is pure standard library (no venv needed) and picks up any new
cycles automatically.
