# SMS + Email + Engagement Plan (April 2026)

## Pilot constraint — REVISED 2026-04-27

**Pilot launches Seychelles, May 11 2026 — 14 days from today.**

User-confirmed decisions:
- Seychelles only (no Tanzania for pilot launch)
- AfricaTalking for SMS
- Meta direct for WhatsApp
- $5,000/month budget (supports 1k students with full headroom)
- Opt-in default for engagement notifications
- Phase order accepted as written

**Compressed 14-day scope:**

| Days | Work |
|---|---|
| Apr 27 | Plan revision (this doc) + Phase 1 starts (email engagement) |
| Apr 28–May 3 | Phase 1 ships: welcome / weekly digests / teacher reports / lesson nudges. User signs up AfricaTalking + sender-ID + Meta WhatsApp. |
| May 4–6 | Phase 2: phone field, OTP via AfricaTalking, login-via-OTP |
| May 7–9 | SMS notifications: lesson reminders, teacher alerts, per-channel preferences, rate limits |
| May 10 | QA + copy polish + deploy verification |
| May 11 | Pilot launch |

**Deferred to post-pilot (start once Seychelles is stable + Tanzania timeline confirmed):**
- Phase 3 (WhatsApp Business onboarding — Meta application started in parallel; the 2–6 week clock runs while we're heads-down on Phases 1+2)
- Phase 4 (WhatsApp tutor)
- Phase 5 (SMS tutor)

These phases stay in the plan below as a reference for post-pilot work. Don't build them before May 11.

---



Strategic plan for adding SMS-based interactions, phone/email verification, and engagement notifications on top of the existing web tutor. Written after the on-device LLM mobile path was paused (see `feedback_on_device_llm_findings.md`).

## TL;DR

- **Email + ACS** is in progress (commit `55a2ba4`). Domain verification pending.
- Build engagement notifications (digests, nudges) on top of email first — fastest value, no new vendor.
- Add **phone verification (OTP)** as the entry point to SMS infrastructure. Cheap, low risk.
- For SMS *tutoring* — recommend **WhatsApp Business API** as the primary channel and **SMS as the universal-fallback**. Pure SMS as the only channel is expensive and pedagogically limited; WhatsApp covers ~80% of the use cases at ~10% of the cost.
- SMS becomes most valuable in **Tanzania** (planned 2nd pilot), less so in Seychelles. Plan accordingly — don't over-build SMS for the Seychelles pilot.

## What the user asked for

1. SMS system in addition to email
2. Phone + email verification for student accounts
3. SMS-based tutoring on top of the existing backend
4. Email used for analytics + engagement to drive platform usage

## What this is for (the strategic case)

The on-device LLM mobile path didn't clear the quality bar. Cloud LLM (Claude) is the only path that delivers pilot-grade tutoring. Cloud requires internet. Many target students don't have reliable internet — especially in Tanzania (planned pilot per `platform_brief_tanzania.md`).

SMS / WhatsApp give us a channel that:
- Works on flaky / 2G connections
- Reaches feature phones (SMS)
- Doesn't require a data plan (SMS, USSD)
- Reaches students who'd otherwise be excluded
- Is async — works around classroom WiFi gaps

This *replaces* what offline-LLM mobile would have given us. Different mechanism, similar effect: the platform reaches students wherever they are.

---

## Africa context — what shapes the design

### Connectivity by country (rough, 2025 data)

| Country | Smartphone penetration | 4G coverage | Avg 1GB cost (% of monthly income) |
|---|---|---|---|
| Seychelles | ~80% (high) | Excellent in urban | ~1% (cheap) |
| South Africa | ~85% | Excellent | ~1.5% |
| Kenya | ~60% | Good in urban | ~3% |
| Ghana | ~55% | Good in urban | ~3% |
| Nigeria | ~50% | Patchy | ~5% |
| Tanzania | ~35–45% | Patchy outside Dar es Salaam | ~6–10% |
| Uganda | ~30% | Patchy | ~8% |
| Rwanda | ~30% | Reasonable in urban | ~5% |

**SMS reach is ~100% across all of these.** Every phone, every network, even 2G.

### What students actually use (qualitative)

- **WhatsApp** is the dominant chat app where smartphones exist. ~95% of smartphone users in Tanzania/Kenya/South Africa. Often the *only* messaging students use.
- **SMS** for OTPs, mobile-money confirmations, and notifications.
- **USSD** (those `*123#` menus) for mobile money + light services. Free or near-free for users; requires telco partnership.
- **Voice calls** for important things; no data needed.
- **Facebook Messenger** is sticky in older demographics; less among students.

### Implications for the pilot

| Channel | Tanzania pilot value | Seychelles pilot value | Cost per interaction |
|---|---|---|---|
| Web tutor (existing) | Limited — most students lack reliable data | High (already serves them) | ~$0 (cloud LLM only) |
| **WhatsApp Business** | **High** — where students already chat | Medium | ~$0.005–0.05/conversation |
| SMS (one-way notifications) | High | Medium | $0.005–0.02/msg (AfricaTalking) |
| SMS (two-way tutoring) | High but expensive | Low (overkill) | $0.20–1.50/session |
| USSD | Highest reach | Low | Free for student, ~$0.001 for us, requires telco partnership |
| App (RN, paused) | Would have been high | High | One-time build |

**Concrete recommendation**: build WhatsApp + SMS-OTP + email engagement now. Defer pure-SMS tutoring until a Tanzania pilot site validates the demand. Defer USSD until we have a telco partner.

---

## Pilot objective fit

Per `project_pilot_design.md` the metrics are:
- Student engagement (sessions completed)
- Learning outcomes (exit-ticket pass rate)
- Teacher trust (teacher-reported usefulness)
- Operational viability (cost per session, support load)

| Feature | Engagement | Outcomes | Teacher trust | Op cost |
|---|---|---|---|---|
| Email engagement (digests, nudges) | ↑↑ | →  | ↑ | tiny |
| Phone OTP at registration | → | → | → (less spam) | tiny |
| WhatsApp tutor | ↑↑↑ | ↑↑ | ↑↑ | medium |
| SMS tutor (full Socratic) | ↑↑ | ↑ | ↑ | high |
| SMS notifications only | ↑↑ | → | → | low |

**Cleanest sequence to maximize pilot ROI**:
1. Email engagement (now) — cheap engagement boost
2. Phone OTP + SMS notifications (next 2 weeks) — verification + nudges over the most reliable channel
3. WhatsApp tutor MVP (4–6 weeks) — for Tanzania expansion; secondary channel in Seychelles
4. SMS-only tutor (only if WhatsApp coverage is insufficient at a target site)

---

## Architecture decisions to make up front

### 1. Provider — outbound SMS

| Provider | Africa reach | Cost (per SMS to TZ) | API/SDK | Notes |
|---|---|---|---|---|
| **AfricaTalking** | Native — best routes | ~$0.008 | Python SDK + REST | Strong in Kenya/TZ/UG; supports SMS + voice + USSD + airtime |
| **Termii** | Strong in Nigeria/West Africa | ~$0.02 | REST | Better for Nigeria/Ghana |
| **Twilio** | Global, less optimized for Africa | ~$0.05–0.15 | Mature SDK | Higher cost but very reliable |
| **MessageBird/Vonage** | Decent | ~$0.04 | Mature | Mid-cost |

**Recommendation: AfricaTalking** — designed for African networks, ~5–10× cheaper than Twilio for the pilot countries, supports USSD as a future option from the same account.

Backup plan: dual-provider with Twilio for failover. Africa's Talking can have outages.

### 2. Provider — WhatsApp Business

WhatsApp Business API access is mediated by Business Solution Providers (BSPs). Options:

| Provider | Setup pain | Notes |
|---|---|---|
| **Meta Cloud API** (direct from Meta) | Medium | Free tier (1k convos/month), then $0.005–0.05 per conversation depending on country. Best long-term. |
| **Twilio for WhatsApp** | Easy (use existing Twilio account) | More expensive than direct Meta but easiest setup |
| **AfricaTalking WhatsApp** | Easy (use AfricaTalking account) | Tied to one account; cheapest in their countries |
| **360dialog** (Berlin-based BSP) | Medium | Africa-friendly, no per-message markup |

**Recommendation: Meta Cloud API direct** — most control, lowest long-term cost, native template-message system for verified business profile. Setup takes ~1 week (Facebook business verification).

### 3. Country numbers + sender IDs

For SMS, need a sender that won't be marked as spam:
- **Alphanumeric sender ID** (e.g. "AITutor") — works in most African countries; needs registration with each country's regulator. Free–$50 per country.
- **Short code** (e.g. "21000") — premium, $500–2000/month per country. For two-way SMS at scale.
- **Local long number** (e.g. +255-78-XXXXX) — $1–5/month per country, accepts inbound SMS.

**Pilot start**: alphanumeric sender ID for Seychelles + Tanzania + Kenya. Add long number per country only when we go live there.

### 4. Two-way SMS handling

Inbound SMS hits a webhook (e.g. `POST /api/sms/inbound/`). The provider sends:
- `from`: phone number (E.164)
- `to`: our sender ID or long number
- `text`: message body
- `messageId`: dedupe key

Our handler:
1. Look up phone → Student record (must be verified)
2. Find or create active TutorSession (one per phone)
3. Run through ConversationalTutor.respond()
4. Strip markdown / shorten reply to N segments
5. Send each segment via `send_sms`

Each inbound SMS triggers an outbound, so cost is ~2× per round-trip. ~5–15 round-trips per "lesson".

---

## Phased plan

### Phase 0 — Email working (in progress)

Currently: Pulumi + ACS code shipped (commit `55a2ba4`). Pending:
- User runs `pulumi up` to provision
- DNS records added at registrar
- `az communication email domain initiate-verification` for the 5 record kinds
- First test send

No new code — see `runbook` in earlier turn.

### Phase 1 — Email engagement (1 week)

Convert email from "exists for password reset" → "drives student engagement."

Notifications to add:
- Welcome email after registration (with link to first lesson)
- Weekly student progress digest ("You completed 2/5 lessons; here are 3 more this week")
- Streak nudges ("You're on a 4-day streak — keep it going")
- Lesson-due reminders (when teacher assigns deadlines)
- "Your teacher reviewed your exit ticket" alerts

Notifications to teachers:
- Weekly class digest (avg competency, students below threshold, students unassessed)
- Flagged session alerts (when validator flags a tutor turn)
- New student joined / withdrew

Implementation:
- New app `apps/notifications/` with `Notification`, `NotificationTemplate`, `NotificationPreference` models
- Per-user opt-out preferences (CAN-SPAM compliant)
- Celery task or `manage.py` cron command running daily/weekly digests
- Templates use Django's email template system + base template with branding

Effort: 3–4 days. No new vendor.

### Phase 2 — Phone verification + SMS notifications (1–2 weeks)

- New `StudentProfile.phone` (E.164 format) + `phone_verified_at` columns
- Registration flow: optional phone field → "Verify with SMS" → OTP code → `phone_verified_at` stamped
- Login: passwordless option via SMS OTP for students who forgot password
- **AfricaTalking signup** + Pulumi config for `AT_USERNAME` + `AT_API_KEY` secrets
- New `apps/notifications/sms.py` — wraps AfricaTalking SDK
- Notification preferences gain "via SMS / via email / both"
- Critical events (exit-ticket reviewed, lesson assigned) duplicated to SMS for opted-in students

Effort: 1–2 weeks. New vendor (AfricaTalking).

Cost preview: 1000 students × 4 SMS/week × $0.01 = $40/week / $160/month. Manageable.

### Phase 3 — WhatsApp Business onboarding (1 week setup + 2 weeks build)

Setup (mostly user-side):
- Verify Meta Business account (Facebook Business Manager)
- Verify the AI Tutor business
- Get approved for WhatsApp Business API
- Get a WhatsApp number (existing or new)
- Submit message templates for approval (welcome, lesson-reminder, exit-ticket-summary)

Build:
- `apps/notifications/whatsapp.py` — Meta Cloud API client
- Webhook receiver at `POST /api/whatsapp/inbound/`
- Send template messages for transactional flows (welcome, reminders)

Effort: 1 week setup (mostly waiting for Meta verification) + 2 weeks build.

### Phase 4 — WhatsApp tutor MVP (3–4 weeks)

The big one — full ConversationalTutor adapted for WhatsApp.

Architecture:
- WhatsApp inbound webhook → look up student by phone → find/create TutorSession
- Run existing ConversationalTutor.respond() with `client_form_factor='whatsapp'` (extends the existing X-Client-Form-Factor pattern)
- Tutor system prompt: WhatsApp-specific format block (1–3 short paragraphs, plain text, can use *bold* but no markdown headers, links work as plain URLs)
- Send response via Meta Cloud API
- Media: tutor signals media via `|||MEDIA:N|||` (existing pattern); WhatsApp send-message includes the image URL → renders inline in WhatsApp

Constraints:
- Response length cap: ~1024 chars (WhatsApp UX preferred; not API limit)
- 24-hour conversation window — outside that, can only send template messages, not free-form
- Each conversation costs ~$0.005–0.05 depending on country category
- Need to send a template (e.g. "Your tutor is back online — reply to continue") if more than 24 hours since student's last message

Effort: 3–4 weeks. Includes prompt tuning, latency measurement, abuse-prevention, opt-out flow.

### Phase 5 — Pure-SMS tutor (3–4 weeks, only if needed)

Implement only if a target site has WhatsApp penetration <50%.

Same architecture as Phase 4 but with SMS:
- Inbound SMS webhook (AfricaTalking)
- ConversationalTutor with `client_form_factor='sms'` — 160-char reply target, no media, no markdown
- Outbound chunked into multiple SMS if needed (rare with tight format)
- Cost monitoring with hard daily caps per student (prevent runaway)

Effort: 3–4 weeks if WhatsApp work is already done (lots of reuse).

---

## Cost model — rough monthly estimates

Assumptions: 1000 active students, 4 sessions/week each, ~10 turns per session.

| Component | Volume / month | Unit cost | Monthly |
|---|---|---|---|
| Email (ACS) | 50K notifications | $0.0025 | $125 |
| SMS notifications | 16K (4/week × 1000 × 4 weeks) | $0.01 | $160 |
| Phone OTPs | 2K (registration + reset) | $0.01 | $20 |
| WhatsApp tutor | 16K conversations | $0.02 (TZ avg) | $320 |
| SMS tutor (if needed, replaces WhatsApp) | 320K SMS (10 round-trips × 16K sessions × 2) | $0.01 | $3,200 |
| Cloud LLM (existing, all channels) | 200K calls | varies | ~$1,000 |

**With WhatsApp as the tutor channel: ~$1,650/month for 1000 students** ($1.65/student/month all-in).

**With SMS-only as the tutor channel: ~$4,500/month for 1000 students** ($4.50/student/month). 2.7× more.

This is why WhatsApp first matters — it's not just better UX, it's an order-of-magnitude cost saving.

---

## Risks + things I want to flag

1. **WhatsApp Business approval can take 2–6 weeks.** Meta is famously slow for non-US/EU businesses. Start the verification *now*, in parallel with everything else.
2. **Sender-ID approval per country.** Tanzania requires Tanzania Communications Regulatory Authority (TCRA) approval for alphanumeric senders — 1–2 weeks. Kenya is similar (CA approval).
3. **GDPR / data residency** — neither WhatsApp nor SMS is encrypted end-to-end for our purposes (we hold the data). Privacy policy needs to reflect this.
4. **Cost runaway** — a student stuck in a tutor loop could burn $5+ in SMS/WhatsApp in an hour. Need hard rate limits per student per day.
5. **Spam classification** — promotional vs transactional messages have different rules. We're transactional/educational; need to avoid alphanumeric sender IDs being flagged.
6. **Phone-number identity** — phone numbers churn (especially prepaid SIMs in Africa). A phone number that worked yesterday may belong to a different person tomorrow. Need a way to re-verify periodically.
7. **Pilot priority** — Seychelles benefits less from SMS/WhatsApp (high smartphone penetration). If Seychelles is the only near-term pilot, we may be over-investing. Tanzania expansion plan needs to be confirmed before committing weeks of SMS work.
8. **Existing engine adaptation** — `ConversationalTutor.respond()` assumes a long-form response. SMS / WhatsApp need different system prompts, different length constraints, different media handling. The existing `X-Client-Form-Factor: mobile` header pattern extends naturally.

---

## Open questions for the user

Before any code, I need:

1. **Tanzania pilot timeline confirmed?** If Tanzania is >6 months out, deprioritize SMS/WhatsApp. If <3 months out, prioritize them now.
2. **Pick SMS provider**: AfricaTalking (recommended) or Twilio (familiar, expensive)?
3. **Pick WhatsApp path**: Meta Cloud API direct (recommended, more setup) or via a BSP like Twilio (faster setup, more expensive)?
4. **Budget ceiling**: monthly target for SMS/WhatsApp/Email combined? Affects how many students we can support before throttling.
5. **Engagement notification opt-in default**: opt-in (CAN-SPAM safe, lower volume) or opt-out (higher engagement, regulatory risk in some jurisdictions)?
6. **Phase ordering preference**: Email engagement first → phone OTP → WhatsApp tutor (my recommended order) or different?

---

## What I'd build first if you just say "go"

Tightest sequence:

1. **Day 1–2**: Phase 1 email engagement scaffold (welcome email + weekly digest + teacher class digest). No new vendors.
2. **Day 3**: Phone field + OTP verification model. Stub the SMS sender behind a feature flag — uses console for dev.
3. **Day 4–5**: AfricaTalking integration. Real OTP delivery. Notification SMS preference.
4. **Day 6+**: Start WhatsApp Business application (Meta-side, takes 2–6 weeks waiting). In parallel, build the inbound webhook + send adapter.
5. **Week 4+**: WhatsApp tutor MVP if Meta approval has landed.

Each phase is shippable on its own, so we can stop at any phase if pilot priorities shift.

---

## What this plan deliberately doesn't include

- USSD interactions (requires telco partnership negotiations — separate plan)
- Voice / IVR tutoring (much more complex; revisit after WhatsApp validates demand)
- Push notifications to a future native app (depends on resuming the React Native work)
- Conversational analytics dashboards (Phase 1 unlocks the data; UI comes later)
- Multi-language support (lessons are in English now; localization is a separate effort)
