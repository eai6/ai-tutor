# AI Tutor

A conversational tutoring platform for secondary school students. Students work
through a lesson with a tutor that asks, marks and adapts; teachers manage
curriculum and see how a class is doing.

In production with the Seychelles Ministry of Education; a Mozambique pilot is
in preparation.

---

## Run it

One command pulls the application; a second starts it. No source code, no
accounts, no API keys.

```bash
mkdir -p ai-tutor && cd ai-tutor

# The deployment files ship inside the image, so they always match it
docker run --rm ghcr.io/eai6/ai-tutor:latest \
  tar -C /app/deploy/compose -c \
      docker-compose.yml Caddyfile env.example backup.sh restore.sh | tar -x

cp env.example .env      # fill in the few REQUIRED values it lists
docker compose pull      # ~5.5 GB, once
docker compose up -d
```

Then open your domain and create the first administrator:

```bash
docker compose exec app python manage.py createsuperuser
```

**Requirements:** a Linux server with 4 vCPU, 8 GB RAM and 60 GB disk; Docker
with the Compose plugin; and a domain pointing at it (HTTPS is issued and
renewed automatically). The published image is x86_64 — on ARM, build from
source instead.

### What you get without configuring anything

- **A curriculum already loaded.** Courses, lessons, steps and exit tickets are
  bundled in the image and imported on first start. No download, so a first run
  works with no internet at all.
- **A tutor that answers without an API key.** Add `--profile local-llm` and a
  local model runs alongside the app. It is a fallback so the platform works out
  of the box — a small local model is weaker at tutoring than a frontier cloud
  model, and slower on a server with no GPU. Add a provider key whenever you
  want, and the local configuration steps aside.

Figures and images are **not** included. Roughly three quarters of lesson steps
reference one, so those steps read with a gap until you add media or upload your
own curriculum.

---

## Configure it

Everything is set in `.env`, and `env.example` explains each entry and what
breaks without it. The four that matter:

| | |
|---|---|
| `SECRET_KEY` | Signs sessions and password resets. Generate one; the app refuses to start with the development default. |
| `ALLOWED_HOSTS` | The hostname students will type. |
| `CSRF_TRUSTED_ORIGINS` | The same, **with the scheme** — `https://…`. Without it every login fails. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | At least one, unless you use the local model. |

**[→ Self-hosting manual](docs/self-hosting.md)** — the full walkthrough for both
a single server and your own AWS account, plus backup and restore, upgrading,
what leaves your network, and a troubleshooting table written from symptoms
("login returns 403 with no error") rather than causes.

---

## Other ways to run it

| | |
|---|---|
| **Your own AWS account** | ECS Fargate + RDS, built by Pulumi. Survives a machine failure without anyone intervening. [Path B in the manual](docs/self-hosting.md). |
| **Offline desktop app** | Runs a model on the classroom machine and never calls out. For schools with no reliable connection. |
| **Offline kiosk** | An NVIDIA Jetson serving its own WiFi hotspot, for a room with no internet at all. [Reference](docs/reference.md). |
| **pip, no Docker** | `pip install ai-tutor`, then `ai-tutor init && ai-tutor serve`. For a server where containers are not allowed — you supply Postgres and TLS. [Path C in the manual](docs/self-hosting.md). |
| **From source** | `git clone`, then `docker compose build`. Also how an ARM server gets an image. |

---

## Documentation

| | |
|---|---|
| [Self-hosting manual](docs/self-hosting.md) | Deploy it, operate it, back it up |
| [Technical reference](docs/reference.md) | Architecture, every app, configuration, routes, testing |
| [Developer guide](docs/developer_guide.md) | Working on the codebase |
| [Teacher guide](docs/teacher_guide.md) · [Student guide](docs/student_guide.md) | Using it |

---

## A note on what leaves your network

Student data — the database, uploaded media, the application — stays wherever
you put it. **The exception is the model.** When a student sends a message, that
message goes to Anthropic, OpenAI or Google to be answered, which means it
leaves your server and your country.

If that is unacceptable under your rules, run the local model instead, or use
the offline desktop build. Both are documented in the manual.

---

## License

See [LICENSE](LICENSE). The bundled curriculum is derived from national syllabus
material and may carry its own terms — check before redistributing it.
