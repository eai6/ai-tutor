# AI Tutor — Mobile (React Native + Expo)

Mobile client for the AI Tutor platform. Talks to the Django REST API at `/api/v1/*`. See `memory/mobile_rn_plan.md` in the repo root for the full plan.

## Stack

- Expo SDK 54 (TypeScript, file-based routing via `expo-router`)
- TanStack Query for server state, Zustand for client state
- `expo-secure-store` for JWT, `expo-sqlite` + Drizzle for offline cache
- `axios` for HTTP with auth/refresh interceptors

## Phase C status (this commit)

Tier 1 screens implemented: login, register, courses list, lessons-by-course, lesson detail, progress, settings. JWT login + refresh works against `/api/v1/auth/*`.

Tier 2 (online tutor chat), Tier 3 (offline tutor + sync), and on-device LLM are not implemented yet — see plan.

## Run locally

```bash
# 1. Start the Django API in another shell:
#    cd .. && python manage.py runserver
# Default API base is http://localhost:8000/api/v1.
# Override at runtime with EXPO_PUBLIC_API_BASE_URL.

# 2. Install + start Expo:
npm install --legacy-peer-deps
npx expo start
```

iOS simulator and Android emulator default to `localhost`. Real devices need the LAN IP of the API host:

```bash
EXPO_PUBLIC_API_BASE_URL=http://192.168.1.42:8000/api/v1 npx expo start
```

## Project layout

```
app/                         # expo-router file-based routes
  _layout.tsx                # root: auth gate, providers, splash
  (auth)/
    login.tsx
    register.tsx
  (app)/
    _layout.tsx              # tab bar
    index.tsx                # courses
    courses/[id].tsx         # lessons in a course
    lessons/[id].tsx         # lesson detail + outline
    progress.tsx
    settings/index.tsx
src/
  api/                       # axios client, endpoint wrappers, types
  components/                # Button, Card, Screen, TextField
  db/                        # Drizzle schema + expo-sqlite client
  lib/                       # constants, secure-store helpers
  stores/                    # zustand auth store
  theme/                     # colors, spacing, typography
```

## Generating types from the live API

The plan target is OpenAPI-driven types. Once the dev server is up:

```bash
npm run schema:gen
```

(Requires `npx openapi-typescript` — install on demand.)
