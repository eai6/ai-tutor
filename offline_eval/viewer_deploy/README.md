# Multi-turn tutoring experiment viewer — Vercel deploy

`index.html` is a single self-contained static page (the experiment viewer:
transcripts + judge scoring + cross-cycle compare). It has **no build step and no
backend** — Vercel just serves the file.

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
