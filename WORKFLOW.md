# Build Workflow & Prompt Playbook

A reusable, project-agnostic workflow for taking any software project from idea to production-ready. Copy this file into the root of every new project, fill in the placeholders, and run the prompts in order.

Each step produces one source-of-truth document. Later steps read earlier documents instead of re-deriving context, which keeps the coding agent grounded and cuts hallucination.

---

## How to use this

1. Work top to bottom. Don't skip a step unless the project genuinely doesn't need it (e.g. a static site may skip auth).
2. Each prompt names the files it must **read first** and the file it must **write**. Honour both.
3. At every **Approval Gate**, stop and review the output before moving on. Fix it in place rather than piling corrections onto the next step.
4. Reuse the earlier documents as context forever. They are the project's memory.
5. Placeholders use `{{DOUBLE_BRACES}}`. Find-and-replace them before running a prompt.

---

## Reference folder convention

All working documents live in a single hidden folder that is **not committed**. It stays on disk so the agent can re-read it every session, but it never clutters the repo or git history.

```
.workflow/
├── 01-idea.md
├── 02-research.md
├── 03-users.md
├── 04-prd.md
├── 05-stack.md
├── 06-architecture.md
├── 06-database.md
├── 06-api.md
├── 07-design.md
├── rules.md           # project rules, local only (Step 8)
├── 09-tasks.md
├── 10-setup.md
├── 11-dev/            # one note per feature
├── 12-testing.md
├── 13-security.md
├── 14-code-review.md
├── 15-preview.md
├── 16-qa.md
└── 17-production.md
```

Add this to `.gitignore`:

```
# AI workflow reference docs (local source of truth, not committed)
.workflow/
```

**The rules file — `.workflow/rules.md` (Step 8).** These are the project-wide rules your coding agent follows. It lives in `.workflow/` with everything else, stays uncommitted, and is used only on your local machine. Since it won't be auto-loaded from a hidden folder, point your agent at it explicitly — either once at the start of a session or inside each prompt (the prompts below already say "read `.workflow/rules.md` first").

> **Note:** everything here is local and uncommitted, the rules file included. If you later want some docs (e.g. PRD or Architecture) committed as real documentation, just move them out of `.workflow/` — nothing in the workflow depends on them staying hidden.

---

## Global rules (every prompt inherits these)

Every prompt below assumes these rules. They're stated once here instead of repeated in each prompt.

- Read the named input documents **before** doing anything. If one is missing, say so and stop.
- Don't invent features, requirements, or facts. Label assumptions explicitly. Flag any decision that needs my approval before proceeding.
- Prefer the simplest reliable option. No over-engineering, no speculative abstractions, no dependencies that aren't justified.
- Separate MVP scope from future scope. Build foundations before dependent features.
- Never hardcode secrets. Keep them in environment variables. Enforce authorization server-side.
- Inspect existing code before changing it. Reuse existing patterns and components. Keep changes focused; don't touch unrelated code.
- Preserve existing behaviour, results, metrics, and APIs unless the task explicitly requires a change.
- Before starting a step, check for a skill or plugin that fits the work — run the `find-skills` skill to see what's installed — and prefer it over improvising. See **Using skills & plugins** below.
- Write clearly and concisely. Technical depth, no filler.

---

## Using skills & plugins

Prefer a skill or plugin over doing specialised work by hand — it's more consistent and less error-prone. At the start of any step, check whether one applies (run `find-skills` to discover what's installed) and use it. Don't force a tool where it doesn't fit; skip it and say why. The tools below set *how* the work gets done; these prompts still set the goal, the constraints, and what to save.

| Tool | Priority | Use for | In the workflow |
|------|----------|---------|-----------------|
| `find-skills` *(skill)* | High | Discover more capabilities | Any step, before improvising |
| `impeccable` *(plugin)* | High | Better frontend design | Step 7 Design, Step 11 frontend UI |
| `design-taste-frontend` *(skill)* | High | Consistent UI decisions | Step 7 Design, Step 11 frontend UI |
| `humanizer` *(plugin)* | High | Cleaner documentation | Any written doc; Docs & Content Polish utility |
| `figma` *(plugin)* | Medium–high | Design-to-code workflow | Step 7 Design; design → code in Step 11 |
| `cowork-plugin-management` *(plugin)* | Situational | Plugin discovery & management | When building or managing plugins |

These are installed in Claude Code (terminal). Keep the per-machine list current in `.workflow/rules.md`, and use `find-skills` to turn up others as you install them.

---

# The prompts

---

## Step 1 — Idea

**Reads:** nothing · **Writes:** `.workflow/01-idea.md`

```
You are a product strategist who turns rough ideas into sharp, buildable concepts.

Follow the Global Rules in WORKFLOW.md.

MY IDEA:
{{RAW_IDEA}}

TASK:
Pressure-test and sharpen this idea into a concept brief. Produce:
- One-sentence pitch
- The core problem and who has it
- Why now / why this is worth building
- The single most important thing it must do well (the wedge)
- 2–3 realistic alternatives a user has today, and why this is better
- Biggest risks and unknowns
- What a minimum lovable version looks like vs. what to deliberately leave out

Challenge the idea where it's weak. If the scope is too broad, say so and propose a tighter cut.

OUTPUT:
Clean Markdown, ready to save as .workflow/01-idea.md. Do not write code.
```

**Approval Gate:** Is the wedge right? Is the MVP cut realistic?

---

## Step 2 — Research

**Reads:** `01-idea.md` · **Writes:** `.workflow/02-research.md`

```
You are a product and technical researcher. Use web search for anything current (competitors, pricing, libraries, APIs, regulations).

Follow the Global Rules in WORKFLOW.md. Read .workflow/01-idea.md first.

TASK:
Research what already exists and what building this realistically takes. Cover:
- Direct and indirect competitors: what they do well, where they fall short, pricing model
- Market gaps this idea could fill
- Technical feasibility: key capabilities needed and whether mature tools/APIs exist for them
- Relevant constraints: platform rules, data/privacy regulations, licensing
- Rough cost and complexity signals (paid APIs, infra, moderation, etc.)
- Open questions to resolve before committing

Cite sources. Separate verified facts from inference. Flag anything that could kill or reshape the idea.

OUTPUT:
Clean Markdown, ready to save as .workflow/02-research.md. Do not write code.
```

**Approval Gate:** Any finding that changes the idea? Fold it back into Step 1 if so.

---

## Step 3 — Define the User

**Reads:** `01-idea.md`, `02-research.md` · **Writes:** `.workflow/03-users.md`

```
You are a UX researcher who defines users crisply enough to drive product decisions.

Follow the Global Rules in WORKFLOW.md. Read .workflow/01-idea.md and .workflow/02-research.md first.

TASK:
Define who this is for. Produce:
- Primary user segment and (if real) secondary segments
- 1–2 concise personas: context, goals, current workflow, frustrations, what success feels like to them
- Core jobs-to-be-done, phrased as "When ___, I want to ___, so I can ___"
- What this user does NOT need (to protect scope)
- Accessibility and device/context assumptions (mobile-first? offline? screen reader?)

Keep personas grounded in the research, not invented demographics. Mark assumptions.

OUTPUT:
Clean Markdown, ready to save as .workflow/03-users.md. Do not write code.
```

**Approval Gate:** Are these the users you're actually building for?

---

## Step 4 — PRD

**Reads:** `01-idea.md`, `02-research.md`, `03-users.md` · **Writes:** `.workflow/04-prd.md`

```
You are a senior Product Manager who takes products from idea to production.

Follow the Global Rules in WORKFLOW.md. Read .workflow/01-idea.md, 02-research.md and 03-users.md first.

TASK:
Write the PRD — the product source of truth. Include:
- Problem, value proposition, target users (summarised from prior docs)
- Core user journeys and user stories
- Features and functional requirements
- Non-functional requirements (performance, reliability, scalability, privacy)
- Pages / screens
- Roles and permissions
- Data requirements
- Integrations
- Loading, empty and error states
- MVP scope, explicit out-of-scope items, and measurable success criteria

Don't invent features or over-engineer. Label assumptions. Flag decisions needing my approval.

OUTPUT:
Clean Markdown, ready to save as .workflow/04-prd.md. Do not write code.
```

**Approval Gate:** This is the contract for everything downstream. Read it carefully.

---

## Step 5 — Choose Tech Stack

**Reads:** `04-prd.md` · **Writes:** `.workflow/05-stack.md`

```
You are a staff engineer choosing a stack you'll have to live with for years.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md first.

TASK:
Recommend the tech stack. For each layer (frontend, backend, database, hosting/runtime, auth, key libraries) give:
- The recommendation
- Why it fits THIS product's requirements and scale
- The main trade-off accepted
- A credible alternative and why it lost

Then:
- State the one or two decisions that matter most and the reasoning
- Note which choices are reversible later and which are hard to change
- Call out anything that needs my approval (paid services, lock-in, licences)

Pick boring, proven technology unless the product truly needs otherwise. Match the stack to the team size and scale implied by the PRD, not to hype.

OUTPUT:
Clean Markdown, ready to save as .workflow/05-stack.md. Do not write code.
```

**Approval Gate:** Approve the stack — it's expensive to change later.

---

## Step 6 — Architecture (+ Database + API)

Architecture produces three linked documents. Run the main prompt first, then the two companions.

### 6a. Architecture

**Reads:** `04-prd.md`, `05-stack.md` · **Writes:** `.workflow/06-architecture.md`

```
You are a senior Software Architect specialising in secure, scalable, maintainable systems.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md and 05-stack.md first.

TASK:
Define exactly how this product is built. Include:
- High-level architecture and how the pieces fit
- Frontend and backend structure
- Data flow through the system
- Folder structure
- Database interaction approach
- Authentication and authorization model
- API architecture
- External services
- AI architecture, if the product uses AI
- Environment configuration
- Error handling strategy
- Security boundaries (what's trusted, what isn't)
- Testing and deployment approach

Choose the simplest architecture that meets the requirements. No microservices or extra infra unless the PRD demands it. Separate MVP needs from future scalability, and say what you'd change to scale later.

OUTPUT:
Clean Markdown, ready to save as .workflow/06-architecture.md. Do not write code.
```

### 6b. Database

**Reads:** `04-prd.md`, `06-architecture.md` · **Writes:** `.workflow/06-database.md`

```
You are a senior Database Architect designing a production data model.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md and 06-architecture.md first.

TASK:
Define the complete data model. Include:
- Database technology and why
- Tables/collections with columns and types
- Primary and foreign keys
- Relationships and an ER diagram (Mermaid)
- Constraints and validation
- Indexes based on the real queries the app will run
- Ownership and authorization per entity
- Row-level security where applicable
- Sensitive-data handling (what's encrypted, hashed, or masked)
- Migration and seed strategy
- Backup and recovery considerations

Model only what the journeys need. No duplicate or speculative tables, no premature optimisation. Make sure the schema supports the security model.

OUTPUT:
Clean Markdown, ready to save as .workflow/06-database.md. Do not write application code.
```

### 6c. API

**Reads:** `04-prd.md`, `06-architecture.md`, `06-database.md` · **Writes:** `.workflow/06-api.md`

```
You are a senior Backend/API architect for production systems.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md, 06-architecture.md and 06-database.md first.

TASK:
Define how the app communicates internally and with external services. Include:
- Endpoints, methods, and purpose
- Authentication and authorization per endpoint
- Request/response formats
- Validation and error conventions (consistent shapes, status codes)
- Pagination and filtering where needed
- External APIs and integrations
- Webhooks and signature verification
- Rate limiting
- Required environment variables
- Data-privacy and failure handling

Define only the APIs the product needs. Never expose secrets client-side. Enforce authorization server-side. Keep conventions consistent and integrations fault-tolerant.

OUTPUT:
Clean Markdown, ready to save as .workflow/06-api.md. Do not write code.
```

**Approval Gate:** Architecture, schema, and API contract all agree with each other and with the PRD.

---

## Step 7 — Design

**Reads:** `04-prd.md`, `03-users.md` · **Writes:** `.workflow/07-design.md`

```
You are a senior product designer and design-systems expert for production web apps.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md and 03-users.md first.

TASK:
Create the design source of truth. Define:
- Visual direction and design principles
- Colour system and typography (as reusable tokens)
- Spacing and layout system
- Reusable components (states included)
- Navigation and forms
- Loading, empty, and error states
- Interactions and animations
- Responsive behaviour and breakpoints
- Accessibility requirements (contrast, focus, keyboard, screen-reader)
- Page-level layout guidance

3D / immersive note: if this product benefits from 3D or rich motion (e.g. a product showcase, game, data viz, or spatial UI), define the 3D direction too — when to use it, the approach/library implied by the stack, performance budget, and graceful fallback for low-power devices. If 3D adds nothing here, say so and skip it rather than forcing it.

Define reusable rules, not one-off styling. Avoid generic AI-SaaS look and needless decoration. Prioritise usability and consistency. Use impeccable and design-taste-frontend for frontend design and consistent UI decisions, and the figma plugin if the design lives in or ships to Figma.

OUTPUT:
Clean Markdown, ready to save as .workflow/07-design.md. Do not write code.
```

**Approval Gate:** Design matches the users and the product, not a template.

---

## Step 8 — Project Rules (rules.md)

**Reads:** all prior docs · **Writes:** `.workflow/rules.md` (local, uncommitted)

```
You are a senior engineer who keeps large codebases consistent across many contributors and AI agents.

Follow the Global Rules in WORKFLOW.md. Read every .workflow/ document first.

TASK:
Write the project rules the coding agent follows for the whole project. Define rules for:
- Code quality and naming
- Project/folder structure (match the architecture doc)
- Component and utility reuse
- Dependency management (when a new dependency is allowed)
- Type safety
- Error handling
- Security and secrets
- Database and API access patterns
- Testing expectations
- Documentation and comments
- Git and change discipline (focused commits, no unrelated edits)

Also state the workflow contract: before changing code, inspect existing code; follow the .workflow/ documents; reuse existing patterns; avoid rewrites and new dependencies unless justified; never expose secrets; make no unrelated changes.

OUTPUT:
Clean Markdown, ready to save as .workflow/rules.md.
```

**Approval Gate:** These rules bind every later step.

---

## Step 9 — Task Breakdown

**Reads:** all prior docs · **Writes:** `.workflow/09-tasks.md`

```
You are a technical lead planning production development.

Follow the Global Rules in WORKFLOW.md. Read every .workflow/ document and .workflow/rules.md first.

TASK:
Break the build into small, ordered phases — no application code yet. For each phase:
- Goal
- Features/tasks (small enough to build and verify independently)
- Files or modules likely affected
- Dependencies / prerequisites
- Database or API changes
- Tests required
- Completion criteria (how we know it's done)

Build foundations before dependent features. Stay inside the MVP scope and the chosen architecture. Introduce no feature or technology not already in the docs.

OUTPUT:
An ordered plan with clear phases, ready to save as .workflow/09-tasks.md. End with the single recommended first task.
```

**Approval Gate:** Approve the plan and the first task.

---

## Step 10 — Setup

**Reads:** `05-stack.md`, `06-architecture.md`, `.workflow/rules.md` · **Writes:** code + `.workflow/10-setup.md`

```
You are a senior full-stack engineer establishing production-ready foundations.

Follow the Global Rules in WORKFLOW.md. Read .workflow/05-stack.md, 06-architecture.md and .workflow/rules.md first. Inspect the existing project if any files already exist.

TASK:
Initialise the project per the approved architecture. Set up:
- Framework and required dependencies
- Folder structure (matching the architecture doc)
- Configuration
- Environment-variable structure (.env.example with names only, no values)
- Base routing
- Core styling / design tokens from the design doc
- Shared utilities and components
- Development scripts
- Linting, formatting, type checking
- Git-ready structure (.gitignore including .workflow/)

Add no application features yet. Add no unnecessary packages. Keep the project runnable after setup.

OUTPUT:
Implement the setup, verify it runs, then write .workflow/10-setup.md reporting what was created, commands used, and any decisions needing approval.
```

**Approval Gate:** Project runs clean. Lint/typecheck pass.

---

## Step 11 — Development

Run the feature loop once per task from Step 9. The sub-prompts below cover specialised work that comes up during development.

### 11a. Feature loop (the main one)

**Reads:** all `.workflow/` docs + `.workflow/rules.md` · **Writes:** code + `.workflow/11-dev/{{FEATURE}}.md`

```
You are a senior full-stack engineer working in an existing production codebase.

Follow the Global Rules in WORKFLOW.md. Read the relevant .workflow/ docs and .workflow/rules.md first. Implement only this approved task:

TASK:
{{TASK_OR_FEATURE}}

Approach:
- Inspect the relevant existing code and dependencies first, then state your approach briefly.
- Implement the feature completely, following the architecture and design system.
- Reuse existing components and utilities.
- Handle loading, empty, success, and error states.
- Validate inputs and enforce authorization.
- Update database/API code when required.
- Add or update the relevant tests.
- Keep changes focused.

Don't rewrite unrelated code or add unnecessary dependencies. Don't silently change requirements. If there's a blocking ambiguity, stop and ask.

OUTPUT:
Implement and test. Report changed files, tests performed, and any remaining issues, and note anything non-obvious in .workflow/11-dev/{{FEATURE}}.md.
```

### 11b. Database & backend wiring

```
You are a senior backend/database engineer.

Follow the Global Rules in WORKFLOW.md. Read .workflow/06-database.md, 06-architecture.md, 06-api.md and .workflow/rules.md first.

TASK:
Connect and implement the backend and database:
- Configure the database securely (credentials in env vars)
- Create the approved schema and migrations
- Implement models / data access
- Implement the required backend services / endpoints
- Add validation and server-side authorization
- Handle transactions and errors
- Add the indexes from the schema
- Add seed/dev data if specified
- Add tests for critical database and backend behaviour

Follow the approved schema. Don't change database structure without justification.

OUTPUT:
Implement and verify. Report migrations, endpoints/services created, tests run, and anything needing approval.
```

### 11c. Authentication & authorization

```
You are a security-focused full-stack engineer building production auth.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md, 06-architecture.md, 06-database.md, 06-api.md and .workflow/rules.md first.

TASK:
Implement secure auth using the approach defined in the architecture doc:
- Signup / login / logout where required
- Session or token management
- Password handling if applicable
- Protected routes and APIs
- Roles and permissions
- Resource-ownership checks
- Unauthorized / forbidden states
- Session expiry and secure handling
- Relevant tests

Never store passwords or tokens insecurely. Never trust client-side authorization. Enforce permissions server-side. Don't replace existing auth without justification.

OUTPUT:
Implement, test, and report the auth flow, protected resources, permissions, changed files, and any security concerns.
```

### 11d. External service / API integration

```
You are a senior integration engineer building reliable production integrations.

Follow the Global Rules in WORKFLOW.md. Read .workflow/06-api.md, 06-architecture.md, 06-database.md and .workflow/rules.md first.

TASK:
Integrate this service:
{{SERVICE_NAME_AND_PURPOSE}}

Implement:
- Secure authentication/configuration (keys in env vars)
- The required API operations
- Request/response handling
- Input/output validation
- Error handling and timeouts
- Rate-limit handling where relevant
- Retries only where safe (idempotent operations)
- Webhooks and signature verification if required
- Logging without exposing sensitive data
- Tests for success and failure cases

Follow the provider's documented behaviour. Make failures graceful. Prevent duplicate operations. Add nothing unrelated.

OUTPUT:
Implement and verify. Report configuration, env vars required, files changed, tests performed, and remaining limitations.
```

### 11e. MCP server connection (only if the project needs one)

```
You are a senior AI engineer experienced with MCP servers.

Follow the Global Rules in WORKFLOW.md. Read .workflow/rules.md and the architecture doc first.

MCP CAPABILITY NEEDED:
{{MCP_SERVER_AND_PURPOSE}}

TASK:
First determine: what tools/resources the MCP exposes, which capabilities this project actually needs, the required configuration and permissions, and the required env vars or credentials. Then configure the connection and verify the required tools work.

Follow least privilege. Enable no unnecessary tools. Never commit secrets. Don't change application architecture unless required.

OUTPUT:
Report connection status, available relevant tools, configuration performed, verification results, and any manual steps required.
```

**Approval Gate (per feature):** Feature works end to end, tests pass, no scope creep.

---

## Step 12 — Testing

**Reads:** `04-prd.md`, `06-api.md`, `06-architecture.md` + current code · **Writes:** `.workflow/12-testing.md`

```
You are a senior QA / test-automation engineer.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md, 06-api.md, 06-architecture.md and the current implementation first.

TASK:
Build and run a practical test strategy for what's implemented. Test:
- Core user journeys and happy paths
- Edge cases and invalid inputs
- Authentication and permissions
- API / database failures
- Loading, empty, and error states
- Important integrations
- Responsive behaviour where relevant

Add unit, integration, and end-to-end tests for critical behaviour.

Prioritise business-critical paths over coverage numbers. Don't change production behaviour just to make tests pass. Investigate failures; don't mask them.

OUTPUT:
Report tests created/run, passed and failed cases, bugs found with severity, and recommended fixes. Save the summary to .workflow/12-testing.md.
```

**Approval Gate:** Critical paths green. Known bugs triaged.

---

## Step 13 — Security Review

**Reads:** `04-prd.md`, `06-architecture.md`, `06-database.md`, `06-api.md`, `.workflow/rules.md` · **Writes:** `.workflow/13-security.md`

```
You are a senior application-security engineer.

Follow the Global Rules in WORKFLOW.md. Audit the project against .workflow/04-prd.md, 06-architecture.md, 06-database.md, 06-api.md and .workflow/rules.md.

TASK:
Run a security audit and fix what you find. Check:
- Authentication and authorization
- Secrets and environment variables
- API and database access
- Input validation and sanitisation
- Injection and XSS
- CSRF where applicable
- File uploads
- Rate limiting and abuse
- Sensitive-data exposure
- Dependencies (known CVEs)
- Error handling and logging (no secrets in logs)
- Client/server trust boundaries

Don't weaken functionality or controls to hide issues. Fix exploitable, high-impact issues first. Keep fixes minimal and compatible with the architecture.

OUTPUT:
Report findings by severity, fixes implemented, tests performed, and remaining risks. Save to .workflow/13-security.md.
```

**Approval Gate:** No open high/critical findings.

---

## Step 14 — Code Review

**Reads:** all `.workflow/` docs + `.workflow/rules.md` + current code · **Writes:** `.workflow/14-code-review.md`

```
You are a senior engineer reviewing a production codebase before release.

Follow the Global Rules in WORKFLOW.md. Review the implementation against every .workflow/ document and .workflow/rules.md.

TASK:
Review for:
- Architecture violations
- Bugs and incorrect logic
- Duplicate or dead code
- Poor abstractions and needless complexity
- Inconsistent patterns
- Unnecessary dependencies
- Weak error handling
- Maintainability issues and technical debt
- Performance concerns

Fix issues that are clearly justified and safe. Don't rewrite working code for style. Preserve behaviour and architecture. Add no unnecessary abstractions or dependencies. Test important changes.

OUTPUT:
Report critical findings, refactors performed, files changed, tests run, and remaining technical debt. Save to .workflow/14-code-review.md.
```

**Approval Gate:** Code is consistent, clean, and matches the docs.

---

## Step 15 — Preview Deployment

**Reads:** `06-architecture.md`, `06-api.md`, `06-database.md`, `.workflow/rules.md` · **Writes:** `.workflow/15-preview.md`

```
You are a DevOps engineer preparing a preview/staging environment (not production).

Follow the Global Rules in WORKFLOW.md. Read .workflow/06-architecture.md, 06-api.md, 06-database.md and .workflow/rules.md first.

TASK:
Make the app run in a preview environment so it can be tested end to end as if deployed. Configure and verify:
- Production-style build
- Preview hosting/runtime (local prod build or a disposable preview environment)
- Environment variables and secrets wiring (preview values, never production secrets)
- A preview/staging database (seeded, isolated from any real data)
- Database migrations applied
- CORS and URLs set for the preview origin
- Build and start commands
- Health check
- Required external services pointed at sandbox/test modes

This is a disposable preview, not production. Use no production credentials or real user data. Don't destroy any existing data.

OUTPUT:
Report how to run the preview, env vars needed, migration status, the preview URL/command, and any manual steps. Save to .workflow/15-preview.md.
```

**Approval Gate:** Preview boots clean and is reachable.

---

## Step 16 — QA Testing

**Reads:** `04-prd.md`, `03-users.md`, `15-preview.md` · **Writes:** `.workflow/16-qa.md`

```
You are a senior QA engineer running acceptance testing against the preview environment.

Follow the Global Rules in WORKFLOW.md. Read .workflow/04-prd.md, 03-users.md and 15-preview.md first.

TASK:
Validate the running preview against the product, as a real user would. Verify:
- Every core user journey from the PRD, start to finish
- Each user story's acceptance criteria
- Permissions and role boundaries in practice
- Error, empty, and loading states in the real UI
- Integrations behaving against sandbox services
- Responsive behaviour across breakpoints
- Obvious accessibility issues (keyboard, focus, contrast)

Test the real behaviour, not the code. Reproduce each issue with clear steps.

OUTPUT:
Report pass/fail per journey, bugs with repro steps and severity, and a go/no-go read on product completeness. Save to .workflow/16-qa.md.
```

**Approval Gate:** All core journeys pass on the preview.

---

## Step 17 — Production Readiness

Not deploying — but the app must be fully deployment-ready, able to handle real traffic, and scalable. This step delivers that readiness plus a final audit.

**Reads:** every `.workflow/` doc + `.workflow/rules.md` + current code · **Writes:** `.workflow/17-production.md`

```
You are an engineering lead signing off production readiness. We are NOT deploying — the goal is that the app is fully ready to deploy, handle real traffic, and scale.

Follow the Global Rules in WORKFLOW.md. Audit the complete project against every .workflow/ document and .workflow/rules.md.

TASK — Part A: make it production-ready (no deploy, no production credentials):
- Verify a clean production build
- Finalise environment-variable and secrets structure (names and docs, no real values)
- Production-grade config (logging, error handling, timeouts, graceful shutdown)
- Database ready for production: migrations, pooling, indexes verified against real queries
- CORS, security headers, HTTPS assumptions, and production URL handling documented
- Health checks and readiness/liveness endpoints
- A monitoring and alerting plan appropriate to the app's real scale (errors, API/DB health, uptime, performance, failed integrations, resource usage) — configured where possible, documented otherwise, with no secrets ever logged
- Backup and recovery approach documented

TASK — Part B: traffic and scale readiness:
- Identify likely bottlenecks under load (hot paths, N+1 queries, unindexed lookups, unbounded responses)
- Confirm pagination, rate limiting, and caching where they matter
- State how each layer scales (stateless services, connection limits, queue/async for heavy work)
- Note the realistic scaling ceiling of the current design and what to change past it
- Run or describe a basic load check on the critical path and report results

TASK — Part C: final readiness audit:
Verify (don't assume) critical requirements, user journeys, UI/UX consistency, functional correctness, auth, data integrity, API/integrations, security, tests/regressions, performance, responsive behaviour, accessibility, and error/loading/empty states.

Make no risky changes automatically. Prioritise blockers. Never use production credentials or real data.

OUTPUT:
A launch verdict — READY, READY WITH WARNINGS, or NOT READY — with findings by severity, required fixes, scale assessment, tests performed, and everything that must be verified manually before a real deploy. Save to .workflow/17-production.md.
```

**Approval Gate:** Verdict is READY (or READY WITH WARNINGS you accept).

---

# Utility prompts

Use these any time, at any step.

## Debugging / Root Cause

```
You are a senior debugging engineer.

Follow the Global Rules in WORKFLOW.md and .workflow/rules.md.

PROBLEM:
{{ERROR_LOG_OR_BUG_OR_SCREENSHOT}}

TASK:
Investigate systematically. Reproduce or trace the failure. Inspect the relevant code, logs, data, and dependencies. Identify the actual root cause and explain why it happens. Implement the smallest correct fix. Test it and check for regressions.

Don't guess or patch randomly. Don't hide errors, weaken validation, remove tests, or touch unrelated code. Preserve existing behaviour unless the bug requires otherwise. If evidence is insufficient, say what's missing.

OUTPUT:
Report root cause, fix implemented, files changed, tests performed, and remaining risk.
```

## Docs & Content Polish

```
You are an editor making technical writing concise, natural, and human — at university-level technical quality.

Follow the Global Rules in WORKFLOW.md. Use the humanizer plugin to catch AI tells (robotic phrasing, filler, forced contrasts).

TASK:
Review and refine the written content across the repository: READMEs, docs, reports, comments, docstrings, notebooks, and config files.
- Cut repetitive, robotic, overly formal, and filler wording while preserving technical depth and meaning.
- Preserve all equations, results, metrics, citations, references, code behaviour, APIs, and functionality. Invent nothing; change no experimental results.
- Keep valid syntax, formatting, links, metadata, and file structure.
- Edit in place. Don't rename, delete, reorganise files, or touch dependencies or generated content unnecessarily.
- Review the git diff and run the relevant checks to confirm changes are writing-only and introduce no errors.

OUTPUT:
Summarise the changes, list modified files, report validation results, and note any skipped files.
```

---

## Quick reference

| # | Step | Reads | Writes |
|---|------|-------|--------|
| 1 | Idea | — | `01-idea.md` |
| 2 | Research | 01 | `02-research.md` |
| 3 | Define the User | 01, 02 | `03-users.md` |
| 4 | PRD | 01–03 | `04-prd.md` |
| 5 | Tech Stack | 04 | `05-stack.md` |
| 6 | Architecture (+DB +API) | 04, 05 | `06-architecture.md`, `06-database.md`, `06-api.md` |
| 7 | Design | 03, 04 | `07-design.md` |
| 8 | Project Rules | all | `.workflow/rules.md` (local) |
| 9 | Task Breakdown | all | `09-tasks.md` |
| 10 | Setup | 05, 06, rules | code + `10-setup.md` |
| 11 | Development | all | code + `11-dev/*.md` |
| 12 | Testing | 04, 06, code | `12-testing.md` |
| 13 | Security Review | 04, 06, rules | `13-security.md` |
| 14 | Code Review | all, code | `14-code-review.md` |
| 15 | Preview Deployment | 06, rules | `15-preview.md` |
| 16 | QA Testing | 03, 04, 15 | `16-qa.md` |
| 17 | Production Readiness | all, code | `17-production.md` |
