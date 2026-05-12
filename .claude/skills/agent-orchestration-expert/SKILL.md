---
name: agent-orchestration-expert
description: Expert on multi-agent LLM orchestration. Auto-loads when designing or evaluating agentic systems — when to split into agents vs keep as one prompt, generator/critic/refiner loops, risk-aware routing, draft→audit→revise, orchestration trace logging, stopping conditions. Covers foundational patterns (ReAct, Reflexion, Self-Refine, Chain-of-Verification, Tree of Thoughts) and production frameworks (LangGraph, AutoGen, CrewAI, Claude Code subagents, OpenAI Swarm). Strongly opinionated about when NOT to use multi-agent — cites recent research (Cemri et al. 2025) showing 17× error amplification in unstructured setups.
---

# Multi-Agent LLM Orchestration — Expert

Opinionated reference for agentic system design. The bias is **conservative**: single agent first, multi-agent only when there's a measured reason. Most "multi-agent systems" are workflows wearing org-chart costumes.

## TL;DR — the rules in order

1. **Start with a single well-prompted call.** Optimize the prompt before adding agents. Anthropic's explicit guidance: "finding the simplest solution possible, and only increasing complexity when needed."
2. **Workflow first, agent only when needed.** Workflows = predefined code paths; agents = dynamic, model-directed control flow. Most production systems should be workflows.
3. **Split into agents only when you can name the bottleneck.** Different tool allowlists, different context bloat profiles, or genuine parallelism. Org-chart-shaped agents are a smell.
4. **Always log the trace, not just the answer.** Multi-agent failure analysis requires the trace — failures are in handoffs and verification skips, not the final string.
5. **Cap retries. Always.** No global iteration budget = unbounded cost.

## Foundational patterns — what each pays off for

### ReAct (Yao et al., 2022)

**Core idea.** Interleave reasoning traces and task-specific actions in a single trajectory. Thought → action (tool call) → observation → thought → action. ([arXiv 2210.03629](https://arxiv.org/abs/2210.03629))

| Apply when | Avoid when |
|---|---|
| Knowledge-grounded QA with retrieval | Pure reasoning with nothing to look up — CoT alone is cheaper |
| Interactive environments (ALFWorld +34% over imitation) | Long action horizons that exceed the context window |
| Tool-augmented tasks where hallucination is the failure mode | Latency-critical paths (each thought-action round is an LLM call) |

**Pitfall: reasoning rot.** The model emits plausible-sounding thoughts that ignore observations and march toward a wrong answer. Mitigate with forced action-first turns or a separate verifier.

### Reflexion (Shinn et al., 2023)

**Core idea.** Verbal reinforcement learning. After each trial, the agent writes a free-text "reflection" on what went wrong, stored in an episodic memory prepended to the next attempt. No weight updates. ([arXiv 2303.11366](https://arxiv.org/abs/2303.11366))

| Apply when | Avoid when |
|---|---|
| Multi-trial tasks with a usable success/failure signal (testable code, agent trajectories) | Single-shot tasks (no retry budget) |
| 91% pass@1 on HumanEval | Noisy signal that doesn't drive useful reflection |
| Reasoning with checkable answers | Latency-critical paths |

**Pitfall.** Reflections devolve into generic "I should be more careful" without specific corrective content. Constrain the reflection prompt to demand a specific hypothesis + specific change.

### Self-Refine (Madaan et al., 2023)

**Core idea.** Same model plays generator, critic, refiner in a tight loop: produce → critique → revise. No external evaluator. ~20% absolute average gain across 7 tasks. ([arXiv 2303.17651](https://arxiv.org/abs/2303.17651))

| Apply when | Avoid when |
|---|---|
| Clear quality dimensions the model can self-articulate (clarity, completeness, tone) | Factual hallucination where the model can't catch its own errors |
| Output is structured text the model can critique | Use Chain-of-Verification instead for factual tasks |

**Pitfall.** Iteration count inflates cost without quality gain after round 2-3. Cap rounds; track per-round delta on a held-out signal before deploying more.

### Chain-of-Verification (Dhuliawala et al., 2023)

**Core idea.** Four steps: (1) draft, (2) plan verification questions, (3) answer each **independently** without seeing the draft, (4) revise. **The independence step is what breaks the hallucination chain.** ([arXiv 2309.11495](https://arxiv.org/abs/2309.11495))

| Apply when | Avoid when |
|---|---|
| Factual long-form generation | Tasks where verification questions need the same hallucinated context |
| List-based questions (Wikidata-style) | Creative tasks with no facts to verify |
| Closed-book QA where confabulation is the main failure |  |

**Pitfall.** Skipping the independence step collapses CoVe into ordinary self-refinement and loses most of the benefit.

### Tree of Thoughts (Yao et al., 2023)

**Core idea.** Reasoning as search. Expand multiple branches at each step, evaluate each (LLM as heuristic), keep promising ones, backtrack. Game of 24: CoT 4% → ToT 74%. ([arXiv 2305.10601](https://arxiv.org/abs/2305.10601))

| Apply when | Avoid when |
|---|---|
| Problems with explicit lookahead/backtracking | Greedy CoT already hits >80% |
| First-decision-matters-most tasks | Cost is multiplicative — branching × depth × eval calls |
| Planning, puzzles, constrained writing | Production latency budgets |

**Pitfall.** Using ToT as a default reasoning strategy. It's a tool for problems where search structure is genuinely present, not a free quality boost.

## Production frameworks — which to reach for

### LangGraph — graph-based state machine

**Model.** Agents are nodes in a directed graph. State is typed and shared. Edges are deterministic Python; the LLM decides routing via tool calls returning `Command(goto=..., update=...)`. ([reference](https://reference.langchain.com/python/langgraph-supervisor))

**Architectures supported:**
- **Supervisor** — central router LLM picks the next agent. One extra LLM call per hop; easy to reason about.
- **Swarm** — agents hand off directly via `Command`. Faster, fewer LLM calls; harder to debug.
- **Network** — any-to-any. Most flexible, least structured.
- **Hierarchical** — supervisor of supervisors.

**Reach for it when.** You can draw the workflow as a graph (branches, loops, checkpoints, human-in-the-loop pauses). State persistence and resume matter.

**Skip it when.** The graph would be a single node with self-loops. You're paying for ceremony.

### AutoGen (Microsoft) — conversational multi-agent

**Model.** Agents post messages to a shared chat; a team controller picks the next speaker. Teams: `RoundRobinGroupChat`, `SelectorGroupChat` (LLM picks next), `MagenticOneGroupChat`, `Swarm`. ([teams tutorial](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html))

**Termination.** `TextMentionTermination("APPROVE")`, `ExternalTermination`, `CancellationToken` for hard abort.

**Reach for it when.** Brainstorming/critique that maps naturally onto conversation (dev + reviewer + tester). Human-in-the-loop as just-another-agent.

**Skip it when.** Strict deterministic pipelines. Conversation metaphor adds tokens without adding capability. Cost-sensitive paths where every agent sees the whole transcript.

### CrewAI — role-based delegation

**Model.** Agents defined by **role**, **goal**, **backstory** — semantic identities. `allow_delegation=True` lets an agent reassign work. Sequential or hierarchical process. ([docs](https://docs.crewai.com/concepts/agents))

**Reach for it when.** Workflows genuinely map onto distinct professional roles where the persona framing measurably improves output.

**Skip it when.** Role names are theater. A "Senior Software Architect" persona vs plain prompt usually shows zero benchmark difference.

**Pitfall.** Mistaking role descriptions for tool-level constraints. Backstory doesn't stop an agent from doing things outside its role — **tool allowlists do**.

### Claude Code subagents (Anthropic)

**Definition.** Markdown file with YAML frontmatter under `.claude/agents/` (project) or `~/.claude/agents/` (user). ([docs](https://code.claude.com/docs/en/sub-agents))

**Key features.**
- **Context isolation** — each subagent runs in its own context window. Verbose output stays in the subagent; only the summary returns to the parent.
- **Tool allowlists/denylists** — restrict per subagent.
- **`description` is the auto-trigger** — Claude matches the task to a subagent's description. Write descriptions for delegation, not for humans.
- **No infinite nesting** — subagents cannot spawn other subagents.
- **Lifecycle hooks** — `PreToolUse`/`PostToolUse`/`Stop` in frontmatter; `SubagentStart`/`SubagentStop` in `settings.json`.

**Reach for it when.**
- Side tasks that would flood the main conversation (test runs, codebase exploration, log analysis).
- Tasks needing stricter permissions than the main thread.
- Reusable workflows ("we always spawn the same kind of worker").

**Skip it when.**
- Tasks needing frequent back-and-forth — startup cost dominates.
- Quick targeted changes — overkill.
- Tasks that share planning context with subsequent steps — fresh context loses the planning.

### OpenAI Swarm — explicit handoffs (educational, not production)

**Model.** Agent = `instructions` + `functions`. Functions returning an `Agent` transfer control. ([GitHub](https://github.com/openai/swarm))

**Reach for it when.** Triage/routing flows. Prototyping multi-agent ideas before committing.

**Skip it when.** Production. Swarm explicitly is not for it. Use the same pattern in LangGraph for durability + observability.

## Design principles

### When to split into multiple agents

Split when:
- Each role needs a different **tool allowlist** (researcher = read-only, implementer = write).
- Each role's verbose intermediate output would **blow up the other's context**.
- You want genuine **parallelism**.

Keep as one prompt with sections when:
- The roles share most of their context anyway.
- Latency matters.
- Handoffs would lose information that's hard to summarize.

**Anti-pattern.** Splitting into agents because the org chart you're modeling has multiple people. Org charts aren't latency budgets.

### Generator / critic / refiner — when it earns its keep

Earns its keep when:
- Explicit, articulable evaluation criteria the critic can apply (compilable code, schema-conformant JSON, factual checklist).
- Iteration measurably moves the quality dial on a held-out set.
- "Almost right" is effectively wrong (code, legal drafts, structured math).

Does NOT earn its keep when:
- Criteria are vague ("make it better").
- Generator and critic share the same blind spot (same model can't catch its own factual errors).
- Quality improvement plateaus after round 1.

**Implementation rules.**
- Critic returns **structured output** (JSON verdict with specific issues), not prose.
- **Hard cap on rounds** (typically 2-3).
- Track per-round delta — if round N doesn't move the needle, stop.

([AWS evaluator-reflect-refine](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/evaluator-reflect-refine-loop-patterns.html), [Anthropic Cookbook — evaluator-optimizer](https://platform.claude.com/cookbook/patterns-agents-evaluator-optimizer))

### Risk-aware routing / cascading

**Pattern.** Cheap model handles the easy 80%; escalate to expensive model only when a confidence/risk signal trips; abstain to human at the top tier. Cascade routing beat pure routing by 4% on RouterBench (80% relative improvement over naive baselines). ([Dekoninck et al.](https://files.sri.inf.ethz.ch/website/papers/dekoninck2024cascaderouting.pdf))

**Signals to dispatch on.**
- Input length, domain tag.
- Semantic similarity to known-hard cases.
- Small classifier confidence.
- Cheap model's self-reported uncertainty (but see pitfall).

**Pitfall.** Routing without fallback. The cheap model's "I'm confident" is unreliable on out-of-distribution inputs — always have an escalation rule that doesn't trust the cheap model's self-assessment alone.

### Trace logging — what to record

Follow [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/). One span per LLM call, per tool call, per retrieval, per subagent spawn.

**Minimum schema for an agentic system:**

```
spawn(agent_type, parent_id)
llm_call(model, prompt_tokens, completion_tokens, latency, cost)
tool_call(name, args, result_size, latency)
audit(decision, criteria, verdict)
revise(reason, prev_version_id)
handoff(from, to, reason)
terminate(condition, success)
```

Standard attributes: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`.

**Pitfall.** Logging only the final answer. Multi-agent failure analysis requires the trace, not the result — the failure is in the handoff or the verification skip, not the final string.

### Stopping conditions — always cap

- **Per-agent retries** (`maxTurns` in Claude Code subagent frontmatter).
- **Per-loop iteration cap** (Self-Refine / evaluator-optimizer: 2-3 rounds).
- **Per-workflow total LLM-call budget** — the critical one. Without it, two agents bouncing under their individual caps can burn arbitrary tokens.
- **Termination signals** — explicit text tokens (AutoGen's `TextMentionTermination`), external cancellation, success heuristic, terminal node in the graph.
- **Escalation to human** when confidence < threshold AND retries exhausted. Treat human deferral as a first-class tier.

## When NOT to use multi-agent — the strongest pattern

The 2025 backlash has receipts. Read these before adding agents.

### Cemri et al. — *Why Do Multi-Agent LLM Systems Fail?* (ICLR 2025)

[arXiv 2503.13657](https://arxiv.org/abs/2503.13657). Analyzed 1600+ traces across 7 frameworks. **MAST** (Multi-Agent System Failure Taxonomy) clusters 14 failure modes into:
- **System design** failures
- **Inter-agent misalignment** failures
- **Task verification** failures

**Unstructured multi-agent networks amplify errors up to 17× vs single-agent baselines.** Production failure rates of 41-86.7% trace back not to model capability but to **coordination architecture**.

### Concrete reasons to skip multi-agent

| Reason | Cost |
|---|---|
| **Latency blow-up** | Each handoff costs 100-500ms; 5-agent chains add >2s of pure orchestration |
| **Error amplification** | 17× over single-agent on unstructured networks |
| **Coordination overhead > gain** | Single-agent often outperforms multi-agent on sequential reasoning |
| **Single well-prompted call sufficient** | Optimize the prompt first |
| **Problem not naturally decomposable** | Splitting coupled tasks forces lossy handoffs |

[Microsoft Cloud Adoption Framework — Single-agent vs Multi-agent](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ai-agents/single-agent-multiple-agents): start single-agent and only graduate when you hit a specific, measured ceiling.

## Recent thinking (2024-2026)

### Compound AI systems (BAIR, Feb 2024)

[The Shift from Models to Compound AI Systems](https://bair.berkeley.edu/blog/2024/02/18/compound-ai-systems/). Zaharia, Khattab, Chen, et al.

**Thesis.** SOTA AI results increasingly come from compound systems (multiple model calls, retrievers, tools), not monolithic models.

**Four arguments:**
1. Scaling has diminishing returns vs system design — pushing 30%→80% on a coding benchmark via system design is cheaper than via training.
2. Control and trust — filters, retrieval grounding, output structure are system properties.
3. Dynamic knowledge — static training can't beat retrieval.
4. Performance flexibility — different components for different latency/cost budgets.

### Anthropic — Building Effective Agents (Dec 2024)

[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents). The canonical short list.

**Workflows vs agents.** Workflows = "predefined code paths." Agents = "dynamically direct their own processes and tool usage." Use workflows for predictability; agents only when flexibility is worth the cost.

**Five named workflow patterns.**
1. **Prompt chaining** — sequential decomposition with gates.
2. **Routing** — classify and dispatch to specialized prompts.
3. **Parallelization** — sectioning or voting.
4. **Orchestrator-workers** — central LLM dynamically delegates.
5. **Evaluator-optimizer** — generator + critic loop.

**Three implementation principles.**
1. **Simplicity** — no abstraction layers without measurable gain.
2. **Transparency** — show planning steps.
3. **ACI** (Agent-Computer Interface) — carefully designed tools with documentation and testing.

**Anti-pattern.** Frameworks that "obscure the underlying prompts and responses, making them harder to debug." Use frameworks to start, drop abstractions as you move to production.

### The 2025 counter-views

Synthesized:
- [IBM — "The year companies stop building AI agents and start running them"](https://www.ibm.com/think/news/companies-stop-building-ai-agents-start-running-them) — most enterprise pilots build but don't ship; the gap is observability, governance, cost predictability, not capability.
- [TDS — "The Multi-Agent Trap"](https://towardsdatascience.com/the-multi-agent-trap/) — agents adopted on aesthetic grounds, not validated need.
- [Microsoft — "Three tiers of Agentic AI"](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/three-tiers-of-agentic-ai---and-when-to-use-none-of-them/4510377) — explicit tier-0 ("don't use agents") as a legitimate design.

**The synthesized counter-view.** Many "agents" are workflows wearing agent costumes; many "multi-agent systems" are single agents wearing org charts. **Validate single-agent first, instrument to find the *specific* bottleneck multi-agent would address, then split only that bottleneck.** Treat agent count as a cost, not a feature.

## Safety rules

❌ **Don't** add agents because you can. Find the bottleneck first.
❌ **Don't** trust frameworks that hide prompts and responses — debuggability is non-negotiable.
❌ **Don't** ship without per-workflow LLM-call budget cap.
❌ **Don't** use multi-agent for latency-critical paths (real-time chat <100ms).
❌ **Don't** assume a critic agent will catch errors a generator agent makes — same model = same blind spot.
❌ **Don't** skip the independence step in Chain-of-Verification.
❌ **Don't** treat persona/backstory as a tool-use constraint — use tool allowlists.

✅ **Do** optimize the single-call prompt first.
✅ **Do** prefer workflows (predefined paths) over agents (dynamic control flow) by default.
✅ **Do** log the full trace, not just the answer.
✅ **Do** cap retries at every level (per-agent, per-loop, per-workflow).
✅ **Do** route cheap-first, escalate on signal, abstain to human at the top tier.
✅ **Do** require structured output from critics, not prose.
✅ **Do** read Anthropic's "Building effective agents" before designing.

## Key sources

**Foundational patterns:**
- [ReAct — arXiv 2210.03629](https://arxiv.org/abs/2210.03629)
- [Reflexion — arXiv 2303.11366](https://arxiv.org/abs/2303.11366)
- [Self-Refine — arXiv 2303.17651](https://arxiv.org/abs/2303.17651)
- [Chain-of-Verification — arXiv 2309.11495](https://arxiv.org/abs/2309.11495)
- [Tree of Thoughts — arXiv 2305.10601](https://arxiv.org/abs/2305.10601)

**Frameworks:**
- [Claude Code subagents docs](https://code.claude.com/docs/en/sub-agents)
- [AutoGen teams tutorial](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html)
- [CrewAI agents docs](https://docs.crewai.com/concepts/agents)
- [LangGraph Supervisor reference](https://reference.langchain.com/python/langgraph-supervisor)
- [OpenAI Swarm GitHub](https://github.com/openai/swarm)

**Strategic guidance:**
- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Compound AI Systems — BAIR](https://bair.berkeley.edu/blog/2024/02/18/compound-ai-systems/)
- [Microsoft — Single-agent vs Multi-agent](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ai-agents/single-agent-multiple-agents)

**Critical research:**
- [Cemri et al. — Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)
- [Cascade routing — Dekoninck et al.](https://files.sri.inf.ethz.ch/website/papers/dekoninck2024cascaderouting.pdf)
- [OpenTelemetry GenAI conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/)

**Pattern playbooks:**
- [AWS — Evaluator-reflect-refine](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/evaluator-reflect-refine-loop-patterns.html)
- [Anthropic Cookbook — Evaluator-optimizer](https://platform.claude.com/cookbook/patterns-agents-evaluator-optimizer)

## Further context

- `architecture-patterns-expert` skill — general architecture principles to anchor agent design
- `codebase-architecture-expert` skill — how this codebase's judges + validator already implement an evaluator pattern (without the multi-agent overhead)
- `tutoring-engine-expert` skill — the conversational_tutor.py engine and its design choices
- `claude-api` skill — for Anthropic SDK specifics when implementing agents
- `memory/eval_benchmark_v2_simplified.md` — applied evaluation pattern for this project's tutor
