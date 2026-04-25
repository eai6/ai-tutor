---
name: nextjs-expert
description: Expert-level Next.js 15+ patterns (App Router, RSC, Server Actions). Auto-loads when working on Next.js code. Covers routing, data fetching, caching, streaming, Server Components vs Client Components, form handling, API routes, middleware, authentication, and production deployment. Use when building a Next.js frontend for this project or other web apps.
paths:
  - "**/next.config.{js,mjs,ts}"
  - "**/app/**/*.{ts,tsx,js,jsx}"
  - "**/pages/**/*.{ts,tsx,js,jsx}"
  - "**/*page.{tsx,jsx}"
  - "**/*layout.{tsx,jsx}"
---

# Next.js Expert

Expert guidance for Next.js 15+ with the App Router. This project doesn't have a Next.js app yet — when one is added (likely as a teacher/admin dashboard alternative or a marketing site), apply these patterns.

## The mental model

App Router is **server-first**. Components render on the server by default (RSC — React Server Components). Only opt into client when you need browser APIs, interactivity, or client-only state.

Default component type per file:
- `page.tsx`, `layout.tsx`, `loading.tsx`, `error.tsx` in `app/` — Server Component
- Anything with `'use client'` at the top — Client Component
- Anything imported FROM a Client Component transitively becomes client-bundled

Rule: **keep the client boundary as deep in the tree as possible**. A `<LikeButton>` should be a Client Component, not the whole page.

## Project structure

```
app/
├── layout.tsx              # Root layout (html, body, providers)
├── page.tsx                # /
├── globals.css
├── (marketing)/            # Route group (doesn't affect URL)
│   ├── about/page.tsx      # /about
│   └── pricing/page.tsx    # /pricing
├── (app)/                  # Authenticated app
│   ├── layout.tsx          # Auth gate + shell
│   ├── dashboard/page.tsx
│   └── courses/
│       ├── page.tsx        # /courses
│       └── [id]/page.tsx   # /courses/42
└── api/
    └── webhooks/
        └── stripe/route.ts
```

Co-locate feature files inside their route segment. Shared primitives in `components/`, shared logic in `lib/`.

## Data fetching

### Server Components (default)

Fetch directly in the component. Next.js deduplicates requests across the render:

```tsx
// app/courses/[id]/page.tsx
async function getCourse(id: string) {
  const res = await fetch(`${API_URL}/courses/${id}`, {
    next: { revalidate: 60 },  // ISR: revalidate every 60s
  });
  if (!res.ok) throw new Error('Failed');
  return res.json();
}

export default async function CoursePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;                // Next.js 15: params is Promise
  const course = await getCourse(id);
  return <CourseView course={course} />;
}
```

Three caching modes:
- `{ cache: 'force-cache' }` — cached indefinitely (default for static fetches)
- `{ next: { revalidate: 60 } }` — ISR (stale-while-revalidate)
- `{ cache: 'no-store' }` — always fresh (dynamic)

Tag-based revalidation (invalidate many fetches at once):
```tsx
fetch(url, { next: { tags: ['course'] } });
// Later:
import { revalidateTag } from 'next/cache';
revalidateTag('course');
```

### Client Components — TanStack Query

For interactive data (filters, mutations, real-time), use TanStack Query — NOT `useEffect + useState`:

```tsx
'use client';
import { useQuery } from '@tanstack/react-query';

export function CourseList() {
  const { data, isLoading } = useQuery({
    queryKey: ['courses'],
    queryFn: () => fetch('/api/courses').then(r => r.json()),
  });
  if (isLoading) return <Skeleton />;
  return <List items={data} />;
}
```

## Server Actions (forms + mutations)

Prefer Server Actions for form submissions over API routes. Fewer layers, type-safe:

```tsx
// app/courses/new/page.tsx
import { revalidatePath } from 'next/cache';
import { redirect } from 'next/navigation';

async function createCourse(formData: FormData) {
  'use server';
  const title = formData.get('title') as string;
  const course = await db.course.create({ data: { title } });
  revalidatePath('/courses');
  redirect(`/courses/${course.id}`);
}

export default function NewCoursePage() {
  return (
    <form action={createCourse}>
      <input name="title" required />
      <button type="submit">Create</button>
    </form>
  );
}
```

For client-side progressive enhancement, use `useFormState` / `useActionState`:

```tsx
'use client';
import { useActionState } from 'react';

export function Form({ action }: { action: (prev: any, formData: FormData) => Promise<any> }) {
  const [state, formAction, pending] = useActionState(action, { error: null });
  return (
    <form action={formAction}>
      <input name="title" />
      <button disabled={pending}>Submit</button>
      {state.error && <p>{state.error}</p>}
    </form>
  );
}
```

## Loading & error states

Next.js auto-wires these from co-located files:

```
app/courses/[id]/
├── page.tsx          # Main content
├── loading.tsx       # Shown while page.tsx is resolving
└── error.tsx         # Shown if page.tsx throws (Client Component)
```

```tsx
// app/courses/[id]/error.tsx
'use client';
export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div>
      <h2>Something went wrong</h2>
      <button onClick={reset}>Try again</button>
    </div>
  );
}
```

For finer control, use `<Suspense>` manually around data-fetching subtrees.

## Streaming + Suspense

Stream the shell first, then fill in slow data:

```tsx
// app/dashboard/page.tsx
import { Suspense } from 'react';

export default function Dashboard() {
  return (
    <>
      <Header />
      <Suspense fallback={<Skeleton />}>
        <SlowWidget />  {/* Fetches its own data */}
      </Suspense>
      <Suspense fallback={<Skeleton />}>
        <AnotherSlowWidget />
      </Suspense>
    </>
  );
}
```

Each `<Suspense>` boundary streams independently. User sees something immediately.

## Middleware (`middleware.ts` at root)

Runs before every matched request. Edge runtime by default — keep it light.

```ts
// middleware.ts
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(request: NextRequest) {
  const token = request.cookies.get('access_token');
  if (!token && request.nextUrl.pathname.startsWith('/app')) {
    return NextResponse.redirect(new URL('/login', request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/app/:path*', '/api/protected/:path*'],
};
```

Use middleware for: auth gates, i18n redirects, A/B testing. NOT for business logic.

## Auth patterns

### Simple: cookie-based sessions

- Store session token as HttpOnly cookie on login
- Read in middleware to gate routes
- Server Components read cookie via `cookies()` from `next/headers`:

```tsx
import { cookies } from 'next/headers';

export default async function DashboardPage() {
  const session = (await cookies()).get('session');
  if (!session) redirect('/login');
  const user = await getUserFromSession(session.value);
  return <div>Hi {user.name}</div>;
}
```

### JWT + refresh

Store access token in an HttpOnly cookie (not localStorage — XSS). Refresh via server route.

### NextAuth.js (Auth.js v5)

Use for OAuth (Google, GitHub, etc.) or email-magic-link flows. Overkill for simple username/password.

## API Routes

Only use when Server Actions don't fit — e.g., webhooks, public APIs, file uploads:

```ts
// app/api/webhooks/stripe/route.ts
import { headers } from 'next/headers';
import Stripe from 'stripe';

export async function POST(req: Request) {
  const sig = (await headers()).get('stripe-signature');
  const body = await req.text();
  const event = stripe.webhooks.constructEvent(body, sig, secret);
  // ... handle event
  return Response.json({ ok: true });
}
```

Supported methods: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`.

## Caching rules (Next.js 15)

Next.js 15 changed defaults — fetches are **uncached by default** unless you opt in. Be explicit:

| Intent | Pattern |
|---|---|
| Static, long cache | `fetch(url, { cache: 'force-cache' })` |
| ISR | `fetch(url, { next: { revalidate: 60 } })` |
| Tag-based invalidation | `fetch(url, { next: { tags: ['key'] } })` + `revalidateTag('key')` |
| Always fresh | `fetch(url, { cache: 'no-store' })` (or just default in 15) |

Server Component caching (`unstable_cache`):
```tsx
import { unstable_cache } from 'next/cache';

const getCourse = unstable_cache(
  async (id: string) => db.course.findUnique({ where: { id } }),
  ['course'],
  { revalidate: 60, tags: ['course'] },
);
```

## Image + font optimization

```tsx
import Image from 'next/image';

<Image
  src="/hero.png"
  alt="Hero"
  width={800}
  height={600}
  priority             // LCP images
  placeholder="blur"
  blurDataURL="..."
/>
```

Fonts:
```tsx
// app/layout.tsx
import { Inter } from 'next/font/google';
const inter = Inter({ subsets: ['latin'] });

export default function RootLayout({ children }) {
  return <html lang="en" className={inter.className}>{children}</html>;
}
```

## Environment variables

- `.env.local` — local dev, gitignored
- `.env` — committed, defaults
- Prefix `NEXT_PUBLIC_` to expose to client (everything else is server-only)

```
NEXT_PUBLIC_API_URL=https://api.example.com    # client + server
DATABASE_URL=postgres://...                     # server only
```

Type them:
```ts
// env.ts
import { z } from 'zod';

export const env = z.object({
  DATABASE_URL: z.string().url(),
  NEXT_PUBLIC_API_URL: z.string().url(),
}).parse(process.env);
```

## Do / don't

✅ **Do**: keep client boundaries deep. Server Components by default.
✅ **Do**: use `<Suspense>` to stream slow subtrees independently.
✅ **Do**: use Server Actions for forms (simpler than API routes).
✅ **Do**: type your env vars with Zod.
✅ **Do**: co-locate `loading.tsx` and `error.tsx` with `page.tsx`.
✅ **Do**: use `unstable_cache` + tag-based revalidation for expensive server computations.

❌ **Don't**: add `'use client'` to a page just because one child needs it. Isolate the interactive part.
❌ **Don't**: fetch the same data in multiple Server Components — use `cache()` or fetch-level dedup.
❌ **Don't**: use `useEffect` to fetch data in Client Components when you could use TanStack Query.
❌ **Don't**: put business logic in middleware. Keep it light — auth checks, redirects.
❌ **Don't**: ship `pages/` and `app/` in the same project if you can avoid it. Pick one (App Router for new).
❌ **Don't**: assume caching defaults from old Next.js versions — 15 changed them.

## Production deploy

Default target: **Vercel**. Zero config, Preview deploys per PR, Edge runtime support.

Alternatives:
- **Cloudflare Workers** (via `@cloudflare/next-on-pages`) — cheaper, more control, no ISR
- **Self-host on Node** — `next build && next start`, reverse-proxy with nginx
- **Docker** — good for integrated deploys (e.g., alongside the Django backend on Azure Container Apps)

If deploying alongside Django on Azure Container Apps: share the nginx/ALB routing. Next.js has its own Container Apps image pattern — standalone build mode: `output: 'standalone'` in `next.config.ts`.

## Testing

- Unit: **Vitest** + **React Testing Library**
- E2E: **Playwright** (best-in-class for Next.js)
- Skip for pilot-stage apps; add as traffic grows

## Common gotchas

- **Server Actions return only serializable data.** No functions, no classes.
- **`params` is a Promise in Next.js 15** — `await params` in pages.
- **Client-only libraries** (jQuery plugins, etc.) must be dynamically imported: `const X = dynamic(() => import('x'), { ssr: false })`.
- **Third-party Providers** (TanStack Query, Theme) go in a `providers.tsx` Client Component wrapping children in `app/layout.tsx`.
- **ISR + runtime='edge'** don't play well — ISR requires Node runtime.

## When to add Next.js to this project

No Next.js work has started yet. Likely use cases:
- A fast marketing/landing site for the tutor platform
- A separate teacher admin panel (split from Django templates)
- A school-facing dashboard

If asked to build one, start by clarifying:
1. What's the user-facing purpose?
2. Will it share auth with the Django backend? (If yes, cookie-based SSO or consume Django's JWT API.)
3. Where's it deployed? (Vercel vs alongside Django on Azure Container Apps.)
4. Does it replace any Django templates, or coexist?
