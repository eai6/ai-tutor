---
name: react-native-expert
description: Expert-level React Native + Expo + TypeScript patterns for the AI Tutor mobile app. Auto-loads when working on files under mobile/. Covers Expo Router, Drizzle + SQLite, TanStack Query, Zustand, JWT auth, on-device LLM integration (llama.rn), offline-first architecture, and the three-layer inference abstraction mirroring the Django backend. Use when writing or modifying React Native code.
paths:
  - "mobile/**/*.{ts,tsx,js,jsx}"
  - "mobile/**/*.json"
  - "mobile/**/*.md"
---

# React Native Expert — AI Tutor Mobile App

Expert guidance for the React Native / Expo / TypeScript mobile app. Companion to `memory/mobile_rn_plan.md` (the execution plan). This skill is the "while you're writing code" guide.

## Stack (locked)

- **Expo SDK 55** with dev client (NOT bare RN, NOT pure Expo Go). Bundles **React Native 0.83** and **React 19.2**. New Architecture (Fabric + Turbo Modules) is always on in SDK 55 — cannot be disabled.
- **Platform minimums**: iOS 15.1+ / Android 7+ / Xcode 26.2+ / compileSdkVersion 36. Worth checking pilot device survey — pre-iPhone 7 and old Androids are out.
- **TypeScript** (required — no JS files)
- **Expo Router** (file-based navigation, Next.js-like)
- **Zustand** for client state (lighter than Redux)
- **TanStack Query v5** for server state (caching, retries, background refetch)
- **Drizzle ORM** over `expo-sqlite` (type-safe SQLite)
- **Axios** with interceptors for JWT auth
- **expo-secure-store** for tokens (Keychain/Keystore)
- **llama.rn** for on-device LLM inference (GGUF format)
- **EAS Build + EAS Submit** for CI/distribution

Rejected: no Redux, no GraphQL, no Nativewind/Tailwind (use StyleSheet.create), no pure Expo Go.

## Project structure

```
mobile/
├── app/                    # Expo Router routes (file-based)
│   ├── (auth)/login.tsx
│   ├── (app)/index.tsx     # home tab
│   └── (app)/tutor/[sessionId].tsx
├── src/
│   ├── api/                # HTTP client, endpoints, generated types
│   ├── db/                 # Drizzle schema + migrations + queries
│   ├── inference/          # InferenceClient interface + implementations
│   ├── model-manager/      # Download, storage, catalog
│   ├── state-machine/      # Policy-as-data tutor runner
│   ├── sync/               # Offline write queue + worker
│   ├── components/
│   ├── hooks/
│   ├── stores/             # Zustand
│   └── theme/
├── plugins/                # Expo config plugins for native modules
├── app.config.ts
├── eas.json
└── tsconfig.json
```

## Key patterns

### 1. Three-layer inference abstraction (mirror of `apps/llm/client.py`)

The mobile app's core architectural pattern. Same shape as Django backend:

```
InferenceClient (abstract interface)
├── CloudApiClient     → POST /api/v1/sessions/<id>/respond/  [online, Django proxies]
├── LlamaRnClient      → llama.rn + GGUF model               [offline, primary]
└── ExecuTorchClient   → react-native-executorch              [offline, fallback]
```

All three implement:
```ts
generate(messages, systemPrompt, opts): Promise<InferenceResponse>
generateStream(messages, systemPrompt, opts): AsyncIterable<string>
```

Engine code calls `resolveClient()` from `src/inference/registry.ts`, which picks the right client based on connectivity + active model. Engine is runtime-agnostic.

**Never** branch on connectivity in UI code. The registry handles it.

### 2. Offline-first data flow

Every user action:
1. Write to local SQLite FIRST
2. Enqueue sync task
3. Return to UI (optimistic)
4. Background worker pushes to server when connected
5. Server response updates local (may no-op)

The UI never waits on network for primary actions. Pattern:

```ts
// hooks/useRespond.ts
export function useRespond(sessionId: string) {
  const db = useDatabase();
  return useMutation({
    mutationFn: async (input: string) => {
      // 1. Write student turn locally
      const studentTurn = await db.insert(session_turns).values({
        id: randomUUID(),
        session_id: sessionId,
        role: 'student',
        content: input,
        client_generated_at: new Date(),
        synced: false,
      }).returning();

      // 2. Generate tutor reply (cloud or on-device via registry)
      const client = await resolveClient();
      const messages = await db.select()...;
      const response = await client.generate(messages, systemPrompt);

      // 3. Write tutor turn locally
      await db.insert(session_turns).values({
        id: randomUUID(),
        session_id: sessionId,
        role: 'tutor',
        content: response.content,
        generated_offline: response.generated_offline,
        client_generated_at: new Date(),
        synced: false,
      });

      // 4. Enqueue sync
      await enqueueSync({ kind: 'turn', session_id: sessionId, ... });

      return response;
    },
  });
}
```

### 3. State management split

- **Server state** → TanStack Query. Everything fetched from API.
- **Client ephemeral UI state** → Zustand (auth store, UI mode, active model).
- **Persistent offline state** → SQLite (Drizzle).

Don't use Zustand for things that should be in SQLite (session turns, progress). Don't use SQLite for things that are ephemeral (is keyboard open).

Example stores:

```ts
// src/stores/auth-store.ts
import { create } from 'zustand';

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  setTokens: (access: string, refresh: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  refreshToken: null,
  user: null,
  setTokens: (access, refresh) => set({ accessToken: access, refreshToken: refresh }),
  logout: () => set({ accessToken: null, refreshToken: null, user: null }),
}));
```

Auth tokens persist via `expo-secure-store`, not Zustand. Hydrate on app start.

### 4. Axios auth interceptors

```ts
// src/api/client.ts
import axios from 'axios';
import * as SecureStore from 'expo-secure-store';

export const apiClient = axios.create({
  baseURL: Config.API_URL + '/api/v1',
  timeout: 30000,
});

apiClient.interceptors.request.use(async (config) => {
  const token = await SecureStore.getItemAsync('access_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401 && !error.config._retry) {
      error.config._retry = true;
      const newToken = await refreshAccessToken();
      if (newToken) {
        error.config.headers.Authorization = `Bearer ${newToken}`;
        return apiClient(error.config);
      }
      // refresh failed → logout
      useAuthStore.getState().logout();
    }
    return Promise.reject(error);
  },
);
```

### 5. Drizzle schema + migrations

```ts
// src/db/schema.ts
import { sqliteTable, integer, text, real } from 'drizzle-orm/sqlite-core';

export const lessons = sqliteTable('lessons', {
  id: integer('id').primaryKey(),
  title: text('title').notNull(),
  pack_downloaded: integer('pack_downloaded', { mode: 'boolean' }).default(false),
  synced_at: integer('synced_at', { mode: 'timestamp' }),
});

// Generate migrations:
// npx drizzle-kit generate --name add_lessons
```

Run migrations at app startup:

```ts
// src/db/client.ts
import * as SQLite from 'expo-sqlite';
import { drizzle } from 'drizzle-orm/expo-sqlite';
import { migrate } from 'drizzle-orm/expo-sqlite/migrator';
import migrations from './migrations/migrations.js';

const sqlite = SQLite.openDatabaseSync('app.db');
export const db = drizzle(sqlite);

export async function runMigrations() {
  await migrate(db, migrations);
}
```

### 6. On-device LLM with llama.rn

```ts
// src/inference/llama-rn-client.ts
import { initLlama, LlamaContext } from 'llama.rn';

export class LlamaRnClient implements InferenceClient {
  private ctx: LlamaContext | null = null;

  static async load(model: MobileInferenceModel): Promise<LlamaRnClient> {
    const path = await getLocalModelPath(model.id);
    return new LlamaRnClient(model.id, path, model.chat_template, model.capabilities);
  }

  private async ensureLoaded() {
    if (!this.ctx) {
      this.ctx = await initLlama({
        model: this.modelPath,
        n_ctx: 8192,          // context tokens
        n_threads: 4,         // CPU threads
        n_gpu_layers: 99,     // Metal (iOS) / Vulkan (Android) acceleration
      });
    }
  }

  async generate(messages, systemPrompt, opts) {
    await this.ensureLoaded();
    const prompt = renderChatTemplate(this.chatTemplate, systemPrompt, messages);
    const result = await this.ctx!.completion({
      prompt,
      n_predict: opts?.maxTokens ?? 512,
      temperature: opts?.temperature ?? 0.7,
      stop: opts?.stop ?? [],
    });
    return {
      content: result.text,
      tokens_in: result.tokens_evaluated,
      tokens_out: result.tokens_predicted,
      latency_ms: result.timings.total_ms,
      model_id: this.modelId,
      generated_offline: true,
    };
  }

  async unload() {
    if (this.ctx) { await this.ctx.release(); this.ctx = null; }
  }
}
```

Chat templates vary per model family — store as strings in `MobileInferenceModel.chat_template` on the server, render client-side with a minimal renderer (~30 lines, don't pull in full Jinja).

### 7. Expo Router patterns

File-based, like Next.js App Router:

```
app/
├── _layout.tsx           # Root: providers, auth gate
├── (auth)/
│   ├── _layout.tsx       # Auth stack layout
│   └── login.tsx
└── (app)/
    ├── _layout.tsx       # Tab layout
    ├── index.tsx         # Home tab
    └── tutor/
        └── [sessionId].tsx
```

Route params:
```ts
import { useLocalSearchParams } from 'expo-router';
const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
```

Navigation:
```ts
import { router } from 'expo-router';
router.push(`/tutor/${sessionId}`);
router.replace('/login');  // when logging out
```

### 8. Auth gate in root layout

```tsx
// app/_layout.tsx
import { Stack, router, SplashScreen } from 'expo-router';
import { useEffect } from 'react';
import { useAuthStore } from '@/stores/auth-store';

SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const token = useAuthStore((s) => s.accessToken);

  useEffect(() => {
    hydrateAuth().then(() => SplashScreen.hideAsync());
  }, []);

  useEffect(() => {
    if (token === null) router.replace('/login');
  }, [token]);

  return <Stack screenOptions={{ headerShown: false }} />;
}
```

## Do / don't

✅ **Do**: type-safe with TypeScript strict mode. `strict: true` in tsconfig.
✅ **Do**: generate API types from OpenAPI (`openapi-typescript` against the Django `/api/v1/schema/`).
✅ **Do**: use `expo-dev-client` from day 1 — you'll need native modules.
✅ **Do**: EAS Build for iOS builds. Local iOS builds require Xcode + signing hell.
✅ **Do**: handle `useNetworkState` for online/offline detection. Show a banner when offline.
✅ **Do**: test on a real device (simulator can't run GGUF models meaningfully).

❌ **Don't**: use `useState` for anything you want to survive a navigation. Use Zustand or SQLite.
❌ **Don't**: pass serializable data through nav params — use Zustand or query cache.
❌ **Don't**: bundle model weights in the app binary — use `react-native-fs` + download on demand.
❌ **Don't**: call Anthropic/OpenAI directly from the app for tutoring. Go through Django `/api/v1/sessions/<id>/respond/`.
❌ **Don't**: store user passwords anywhere on the device. JWT only, in `expo-secure-store`.
❌ **Don't**: block UI on network in the tutor flow. Always local-first.

## EAS Build setup

```json
// eas.json
{
  "cli": { "version": ">= 10.0.0" },
  "build": {
    "development": {
      "developmentClient": true,
      "distribution": "internal"
    },
    "preview": {
      "distribution": "internal",
      "ios": { "simulator": false }
    },
    "production": {
      "autoIncrement": true
    }
  },
  "submit": {
    "production": {
      "ios": { "appleId": "...", "ascAppId": "..." },
      "android": { "serviceAccountKeyPath": "./google-play-key.json" }
    }
  }
}
```

Commands:
```bash
eas build --profile development --platform ios      # dev build
eas build --profile preview --platform all          # preview/TestFlight
eas submit --profile production --platform ios      # App Store / TestFlight
```

## Testing

- `jest` + `@testing-library/react-native` for unit tests
- Skip E2E (Detox/Maestro) for v1 pilot — not worth the setup cost
- Test the inference adapters with a mock client, not a real model
- Smoke-test on at least one real iOS + one real Android device before any build ships

## When stuck

- `memory/mobile_rn_plan.md` — full execution plan with all the decisions
- `memory/offline_mobile_architecture.md` — framework-agnostic arch decisions
- llama.rn GitHub issues — community-maintained, check there for known limitations
- Expo Discord / forums — active, usually responsive
- Ask the user before adopting a new library — stack is deliberately minimal
