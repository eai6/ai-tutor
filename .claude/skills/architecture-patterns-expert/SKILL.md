---
name: architecture-patterns-expert
description: Expert on general software architecture principles — when to apply hexagonal, layered, modular monolith, vertical slices; dependency inversion vs DI; when NOT to abstract (Rule of Three, Metz's "Wrong Abstraction"); schema modeling (STI vs polymorphic vs composition); plugin architectures. Auto-loads when designing new systems, evaluating refactors, debating module boundaries, or reviewing abstraction proposals. Grounded in Fowler, Cockburn, Metz, Vernon, Seemann, plus recent (2024-2026) thinking on modular monoliths and the microservices retreat.
---

# Software Architecture Patterns — Expert

Opinionated reference for architectural decisions. The bias is **conservative** — favor concrete duplication over wrong abstractions, modular monoliths over microservices by default, and dependency inversion only where boundaries genuinely matter.

## TL;DR — the rules in order

1. **Start with a single deployable.** Modular monolith first; extract a service only when there's a *measured* reason (different scaling, different team, different release cadence, different security boundary). Microservices-by-default is over.
2. **Duplicate three times before extracting.** Two similar pieces of code rarely share a *reason* to change. The third occurrence reveals the shape; the abstraction emerges from real usage, not anticipation.
3. **Invert dependencies across architectural boundaries, not everywhere.** Domain → infrastructure: invert. Two internal modules: just import.
4. **Layers are about dependency direction, not file folders.** A "layered" architecture where the domain imports `from django.db import models` is broken regardless of folder structure.
5. **Plugin architectures are for unknown third parties.** If two internal modules need to talk, they import. Plugins solve "many implementations, none yet known."

## Hexagonal architecture / Ports & Adapters

**Core idea.** Cockburn's pattern ([alistair.cockburn.us/hexagonal-architecture](https://alistair.cockburn.us/hexagonal-architecture)) puts application logic at the center, with "ports" defining purposeful conversations and "adapters" providing technology-specific implementations (DB, HTTP, CLI, tests). Goal: an application that can be "equally driven by users, programs, automated tests or batch scripts."

**When to apply.**
- Multiple inbound interaction modes hitting the same use cases (HTTP + queue + scheduled job + CLI).
- Persistence choice is genuinely in flux or you need to test domain logic without infra.
- Each bounded context in a DDD system.

**When NOT to apply.**
- CRUD apps with one UI, one DB, no real domain. Most line-of-business apps.
- Anything where the port/adapter ceremony exceeds the actual domain logic.

**Pitfall: "hexagonal theater".** Creating ports/adapters that have exactly one implementation forever. The value is optionality — if you never exercise it, you paid for nothing. **Test:** if you can't name a plausible second implementation, don't introduce the port.

## Layered architecture

**Core idea.** Stack as Presentation → Application → Domain → Infrastructure. Each layer depends only downward. Fowler's [Layering Principles](https://martinfowler.com/bliki/LayeringPrinciples.html) reduce to: high cohesion within a layer, low coupling between layers, layers agnostic of consumers.

**The critical refinement.** In classic layering, domain depends on data source. In hexagonal/clean ([Uncle Bob, *The Clean Architecture*](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)), you invert that — domain doesn't depend on persistence. A mapper sits between them. **This single inversion is what makes layering useful versus theater.**

**When to apply.** Most apps benefit from at least 2-3 layers (web/domain/data). Especially when layers genuinely change at different rates.

**Common violations.**
- Domain layer importing ORM-specific types (`from django.db import models` in pure business logic).
- Upper layer reaching into a layer two below it (skip-layering).
- The fix is usually dependency inversion, not stricter folder enforcement.

## Dependency Inversion Principle

**Core idea.** The "D" in SOLID: high-level modules shouldn't depend on low-level modules; both depend on abstractions. [Mark Seemann](https://blog.ploeh.dk/2025/01/27/dependency-inversion-without-inversion-of-control/) is emphatic that **DIP and DI are different things** — DIP is the architectural principle, DI is just one technique (constructor injection, containers).

**DIP without DI.** Factory functions, partial application, module-level imports of abstract modules. Containers are optional.

**When to invert.**
- The dependency crosses an architectural boundary you care about (domain → infrastructure).
- The concrete is hard to instantiate in tests.
- You have or genuinely expect a second implementation.

**When to let dependencies flow naturally.**
- Standard-library calls (`json.dumps`, `datetime.now`).
- Pure utilities.
- Stable third-party libraries used the same way everywhere.

**Pitfall: header interfaces.** A one-method `IFooService` that exists solely to be mockable, with a single `FooService` implementation forever. Pure tax.

## When to introduce an abstraction

Three converging heuristics:

| Heuristic | Source | Rule |
|---|---|---|
| **Rule of Three** | folklore / Hunt & Thomas | Wait until duplication appears three times. Two sites are usually coincidental. |
| **YAGNI** | Kent Beck, [Fowler bliki](https://martinfowler.com/bliki/Yagni.html) | Don't add capability for presumptive features. |
| **The Wrong Abstraction** | [Sandi Metz](https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction) | "Duplication is far cheaper than the wrong abstraction." |

**YAGNI nuance.** Fowler explicitly: "yagni only applies to capabilities built into the software to support a presumptive feature, it does not apply to effort to make the software easier to modify... Yagni is not a justification for neglecting the health of your code base." Refactoring, tests, naming, decomposition are NOT YAGNI violations.

**When to abstract.**
- Third duplication AND the three sites share the same *reason* to change.
- You can name the abstraction in a single concrete noun/verb from the domain.
- Removing it would require parallel edits at 3+ sites.

**When NOT to abstract.**
- Two sites that look similar but change for different reasons (coincidental).
- You can't name it without `Manager`, `Helper`, or `Util`.
- The parameter list is already growing flags like `if mode == 'foo'`.

**The wrong-abstraction death spiral** (Metz). Programmer A extracts duplication. B has a near-fit, adds a parameter + conditional. C adds another. Eventually: a condition-laden procedure interleaving vaguely associated ideas. **Prescription: inline it back and let duplication show you the right shape.**

## Module boundaries / modular monolith

**Core idea.** Conway's Law: "an organization will design systems that mirror its communication structure." Module boundaries should map to bounded contexts AND to team ownership. Shopify's Ruby monolith uses [Packwerk](https://shopify.engineering/shopify-monolith) to enforce explicit `package.yml` boundaries — modular monolith, not microservices.

**When modules should be in-process.**
- Same team owns them.
- Same scaling needs.
- Transactional consistency required.
- Latency-sensitive call paths.
- **Default to in-process.** Extraction is cheap when boundaries are clean.

**When modules should be services.**
- Different scaling profile (one CPU-bound, one I/O-bound).
- Different teams with different release cadences.
- Different languages genuinely required.
- Security/compliance isolation.

**Pitfall: distributed monolith.** Kelsey Hightower's diagnosis — "50 deployables that form a distributed monolith — the same thing as before, but instead of function calls and class instantiation, they're initiating things and throwing it over a network." All the coupling, none of the benefits.

## Schema design — inheritance, composition, tagging

Three modeling choices for "X comes in different flavors":

| Approach | When | Cost |
|---|---|---|
| **Single Table Inheritance** | Shallow hierarchy, mostly-shared columns, subclasses substitutable in queries | Wasted columns when subclasses diverge |
| **Polymorphic association** | Linking one entity to many *unrelated* types ("attachable to anything") | Loses FK constraints; slower joins |
| **Composition** | Behavior varies by *configuration*, not by type | Indirection; more objects |
| **Tag/enum column** | 2-3 cases, no per-type columns or behavior | Brittle if cases multiply |

**Pick by Liskov.** If subtypes can't be substituted behaviorally, inheritance is wrong. Rectangle/Square is the canonical violation — mathematically a square is a rectangle, but `setWidth()` on a Square must also change height, breaking caller expectations.

**Prefer `has-a` over `is-a` when in doubt.** Composition is more refactorable; inheritance hierarchies are sticky.

## Plugin / extension architectures

Mature platforms expose extension via three converging mechanisms:

| Mechanism | Example | When |
|---|---|---|
| **Manifests / registries** | VS Code [Contribution Points](https://code.visualstudio.com/api/references/contribution-points) | "I contribute these commands/views" — declarative, lazy-activated |
| **Hooks / signals** | Django signals, VS Code activation events | "React to this named event" |
| **Abstract base classes** | Django auth backends, Python entry points | "Provide an implementation of X" |

**When to apply.** Multiple unknown third parties extend you; extension points are stable but implementations vary; you want lazy loading.

**When NOT to apply.** Two internal modules talking — just import each other. Plugin architecture has real cost: indirection, registry lifecycle, contract versioning.

**Pitfall: too-narrow hooks** (one hook for one customer's need that no one else uses) or **too-broad hooks** ("on_anything" with no contract). Good hooks have a clear semantic name + stable payload.

## Vertical slices vs horizontal layers

The 2024-2026 consensus inside a module: organize by **feature**, not by **layer**.

**Horizontal layering (older default):**
```
app/
  controllers/
    order_controller.py
  services/
    order_service.py
  repositories/
    order_repository.py
```

**Vertical slicing (newer default):**
```
app/
  orders/
    routes.py
    service.py
    repository.py
    types.py
  shipping/
    routes.py
    ...
```

Each slice owns its end-to-end stack. Horizontal layers can still exist *inside* a slice — layered structure within a feature module is fine. ([Milan Jovanović — Vertical Slices in Modular Monolith](https://www.milanjovanovic.tech/blog/where-vertical-slices-fit-inside-the-modular-monolith-architecture))

**When vertical slices pay off.** Most apps. Easier to onboard, easier to delete a feature, easier to map to team ownership.

**Pitfall.** Adopting "vertical slices" as license to skip all shared abstractions — slices still need shared domain primitives. Don't recreate `User` in every slice.

## Recent thinking (2024-2026) — the new consensus

The field has visibly shifted:

1. **Microservices-by-default is over.** Sam Newman added "Whom They Might Not Work For" to *Building Microservices, 2nd ed.* The [Prime Video case study](https://devops.com/microservices-amazon-monolithic-richixbw/) (90% cost cut by moving serverless microservices back to a monolith) became famous — though Adrian Cockroft's [response](https://adrianco.medium.com/so-many-bad-takes-what-is-there-to-learn-from-the-prime-video-microservices-to-monolith-story-4bd0970423d4) is required reading: it wasn't a universal verdict, just one workload.
2. **Modular monolith is the new default.** Greenfield projects, almost everyone under ~50 engineers.
3. **Enforce module boundaries with tooling** (Packwerk, ArchUnit, Spring Modulith). Docs alone don't enforce anything.
4. **Conway's Law isn't optional.** If module boundaries don't match team boundaries, one of them will move.

## Safety rules / red flags

❌ **Don't abstract on the second occurrence.** Wait for the third.
❌ **Don't introduce ports/adapters for testing alone** — use real fakes or in-memory implementations. Header interfaces are tax.
❌ **Don't extract a service to "decouple"** — it adds coupling (network, schema, versioning) plus latency. Decouple in-process first.
❌ **Don't reach across vertical slices.** If `orders/` needs `shipping/` internals, the boundary is wrong or there's a missing shared module.
❌ **Don't enforce layering with docs.** Use linters / ArchUnit / Packwerk.
❌ **Don't use inheritance when composition would do.** Especially in schema design.
❌ **Don't add a plugin system for two known callers.**

✅ **Do** start with a single deployable.
✅ **Do** invert dependencies across architectural boundaries (domain ↔ infrastructure).
✅ **Do** let duplication accumulate until the abstraction's shape is obvious.
✅ **Do** map module boundaries to team boundaries.
✅ **Do** organize by feature (vertical slice) before layer.
✅ **Do** delete the wrong abstraction by inlining — don't add another parameter to "fix" it.

## Key sources

- Cockburn, [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture)
- Fowler, [Layering Principles](https://martinfowler.com/bliki/LayeringPrinciples.html), [Presentation Domain Data Layering](https://martinfowler.com/bliki/PresentationDomainDataLayering.html), [Yagni](https://martinfowler.com/bliki/Yagni.html)
- Uncle Bob, [The Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- Mark Seemann, [Dependency inversion without inversion of control](https://blog.ploeh.dk/2025/01/27/dependency-inversion-without-inversion-of-control/); *DI Principles, Practices, and Patterns* (Manning)
- Sandi Metz, [The Wrong Abstraction](https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction)
- Vaughn Vernon, *Implementing Domain-Driven Design*
- Sam Newman, *Building Microservices, 2nd ed.* (O'Reilly, 2021)
- Shopify, [Deconstructing the Monolith](https://shopify.engineering/deconstructing-monolith-designing-software-maximizes-developer-productivity)
- Milan Jovanović, [Vertical Slices in Modular Monolith](https://www.milanjovanovic.tech/blog/where-vertical-slices-fit-inside-the-modular-monolith-architecture)
- VS Code, [Contribution Points](https://code.visualstudio.com/api/references/contribution-points)
- Kubernetes, [Operator pattern](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/)

## Further context

- `codebase-architecture-expert` skill — how this project's actual architecture compares
- `agent-orchestration-expert` skill — multi-agent specifically
- `django-expert` skill — Django-specific patterns
