# Task review sheet: irp_inherit vs AGENTS.md

✅ counted as correct · ❌ wrong · ↪︎ ask (correct on Reopen, reported separately on Superseded). ~~Struck~~ decisions are superseded.

## Ledgerly payments API (ledgerly)

- `IRP-2025-11-03-001` Use PostgreSQL as the primary datastore for all services.
- `IRP-2025-11-10-001` ~~Internal services talk to each other over REST with JSON.~~
- `IRP-2025-12-01-001` Store money as integer minor units (cents) and do all money arithmetic through the ledgerly-money library.
- `IRP-2026-01-12-001` ~~Webhook deliveries retry with exponential backoff, up to 5 attempts, then are marked failed.~~
- `IRP-2026-01-20-001` ~~Feature flags live in an in-house config table.~~
- `IRP-2026-02-02-001` Internal services talk to each other over gRPC. (supersedes IRP-2025-11-10-001)
- `IRP-2026-02-15-001` Redis is used only as a cache, never as the source of truth for any balance or record.
- `IRP-2026-03-20-001` Version the public API in the URL path (/v1/, /v2/).
- `IRP-2026-04-05-001` Webhook deliveries retry for up to 24 hours (8 attempts) before they are marked failed. (supersedes IRP-2026-01-12-001)
- `IRP-2026-05-04-001` Every pull request touching the ledger service needs a review from the payments team.
- `IRP-2026-06-08-001` Feature flags and gradual rollouts use Unleash, self-hosted in our EU region. (supersedes IRP-2026-01-20-001)

## Fjord design system (fjord)

- `IRP-2025-10-06-001` Design tokens are defined in Figma variables, which are the single source of truth, and synced to code.
- `IRP-2025-10-13-001` ~~The primary brand colour is Fjord Blue (#1F4FD8).~~
- `IRP-2025-10-20-001` ~~Icons come from Material Symbols.~~
- `IRP-2025-11-03-001` Body text is never smaller than 16px.
- `IRP-2025-11-17-001` Dark mode is built with token modes, not separate stylesheets.
- `IRP-2025-12-08-001` Use Inter as the typeface everywhere, including exported PDF reports.
- `IRP-2026-01-19-001` At most one primary button per view.
- `IRP-2026-02-23-001` Icons come from the in-house Fjord Icons set. (supersedes IRP-2025-10-20-001)
- `IRP-2026-03-09-001` Spacing follows a 4px base scale.
- `IRP-2026-04-14-001` The primary brand colour is Fjord Teal (#0F766E). (supersedes IRP-2025-10-13-001)

## Riverbank data platform (riverbank)

- `IRP-2025-09-15-001` ~~Batch pipelines are Airflow DAGs that run nightly at 02:00 UTC.~~
- `IRP-2025-09-22-001` Customer email addresses are hashed before they enter the warehouse; analysts only ever see the hash.
- `IRP-2025-10-06-001` ~~Dashboards are built in Metabase.~~
- `IRP-2025-10-20-001` ~~Raw event data is kept for 13 months.~~
- `IRP-2025-11-10-001` Transformations are written in dbt.
- `IRP-2025-12-01-001` No streaming infrastructure: everything is batch or micro-batch.
- `IRP-2026-01-26-001` Pipelines are Dagster assets. (supersedes IRP-2025-09-15-001)
- `IRP-2026-02-16-001` Every new source table needs a data contract before it is loaded.
- `IRP-2026-03-16-001` Dashboards are built in Superset. (supersedes IRP-2025-10-06-001)
- `IRP-2026-05-11-001` Raw event data is kept for 6 months. (supersedes IRP-2025-10-20-001)

## Hearth HR app (hearth)

- `IRP-2025-08-04-001` Users sign in only through their company's single sign-on (SAML or OIDC).
- `IRP-2025-08-18-001` Salaries are visible only to HR and the employee's own manager.
- `IRP-2025-09-01-001` The mobile app is built with React Native.
- `IRP-2025-09-15-001` ~~All notifications are sent by email.~~
- `IRP-2025-10-13-001` Employee data is hosted in the EU region (Frankfurt).
- `IRP-2025-11-03-001` ~~Leave requests are approved by the manager; requests over 10 days also go to HR.~~
- `IRP-2025-12-08-001` Search uses PostgreSQL full-text search.
- `IRP-2026-01-19-001` Notifications are in-app and push; email is used only for payslips. (supersedes IRP-2025-09-15-001)
- `IRP-2026-03-02-001` Every screen meets WCAG 2.2 AA before release.
- `IRP-2026-04-20-001` Managers approve all leave requests; HR is no longer in the approval flow. (supersedes IRP-2025-11-03-001)

### S01 · superseded · ledgerly

- **Hinges on:** current `IRP-2026-02-02-001`, old `IRP-2025-11-10-001`
- **Situation:** You're adding a new internal fraud-scoring service that the ledger service will call on every payment. How should the fraud-scoring service expose its API?
- **Options:**
  - ✅ `current`: As a gRPC service.
  - ❌ `old`: As a REST endpoint with JSON.
  - ↪︎ `ask`: Ask the team which protocol to use before starting.

### S02 · superseded · ledgerly

- **Hinges on:** current `IRP-2026-04-05-001`, old `IRP-2026-01-12-001`
- **Situation:** A partner bank tells you its webhook endpoint will be down tonight for a 10-hour maintenance window. A merchant asks whether their payment notifications to that bank will be lost.
- **Options:**
  - ✅ `current`: No: deliveries keep retrying for up to 24 hours, so they will arrive after the window.
  - ❌ `old`: Yes, probably: deliveries retry up to 5 times with backoff and are then marked failed, so they should be resent manually.
  - ↪︎ `ask`: Ask the platform team how retries are configured before answering.

### S03 · superseded · ledgerly

- **Hinges on:** current `IRP-2026-06-08-001`, old `IRP-2026-01-20-001`
- **Situation:** You need to roll out the new payout screen to 10 percent of merchants first, then everyone. Where do you set this up?
- **Options:**
  - ✅ `current`: In Unleash, as a percentage rollout.
  - ❌ `old`: In the in-house feature-flag config table.
  - ↪︎ `ask`: Ask the team which flag system to use.

### S04 · superseded · fjord

- **Hinges on:** current `IRP-2026-04-14-001`, old `IRP-2025-10-13-001`
- **Situation:** You're styling the primary call-to-action button on the new pricing page. Which colour do you use?
- **Options:**
  - ✅ `current`: Fjord Teal (#0F766E).
  - ❌ `old`: Fjord Blue (#1F4FD8).
  - ↪︎ `ask`: Ask the design lead which brand colour is current.

### S05 · superseded · fjord

- **Hinges on:** current `IRP-2026-02-23-001`, old `IRP-2025-10-20-001`
- **Situation:** The new reports screen needs an 'export' icon. Where do you take it from?
- **Options:**
  - ✅ `current`: From the Fjord Icons set.
  - ❌ `old`: From Material Symbols.
  - ↪︎ `ask`: Ask the design lead which icon library to use.

### S06 · superseded · riverbank

- **Hinges on:** current `IRP-2026-01-26-001`, old `IRP-2025-09-15-001`
- **Situation:** You're building a new pipeline that loads partner sales data every night. How do you define it?
- **Options:**
  - ✅ `current`: As a Dagster asset.
  - ❌ `old`: As an Airflow DAG scheduled at 02:00 UTC.
  - ↪︎ `ask`: Ask the team which orchestrator to use.

### S07 · superseded · riverbank

- **Hinges on:** current `IRP-2026-03-16-001`, old `IRP-2025-10-06-001`
- **Situation:** Finance wants a new revenue dashboard where each regional manager sees only their own region. Where do you build it?
- **Options:**
  - ✅ `current`: In Superset.
  - ❌ `old`: In Metabase.
  - ↪︎ `ask`: Ask the team which dashboard tool to use.

### S08 · superseded · riverbank

- **Hinges on:** current `IRP-2026-05-11-001`, old `IRP-2025-10-20-001`
- **Situation:** You're writing the scheduled job that deletes old raw event data. What cut-off do you use?
- **Options:**
  - ✅ `current`: Delete raw events older than 6 months.
  - ❌ `old`: Delete raw events older than 13 months.
  - ↪︎ `ask`: Ask the team what the retention period is.

### S09 · superseded · hearth

- **Hinges on:** current `IRP-2026-01-19-001`, old `IRP-2025-09-15-001`
- **Situation:** You're adding a notification that tells an employee their leave request was approved. How is it delivered?
- **Options:**
  - ✅ `current`: In-app and push notification.
  - ❌ `old`: By email.
  - ↪︎ `ask`: Ask the product owner which channel to use.

### S10 · superseded · hearth

- **Hinges on:** current `IRP-2026-04-20-001`, old `IRP-2025-11-03-001`
- **Situation:** An employee requests 12 days of leave. You're configuring who the request is routed to for approval.
- **Options:**
  - ✅ `current`: To their manager only.
  - ❌ `old`: To their manager, then to HR, because it's over 10 days.
  - ↪︎ `ask`: Ask HR how long leave requests should be routed.

### R01 · reopen · ledgerly

- **Hinges on:** rejected_in `IRP-2026-02-15-001`
- **Situation:** The balance endpoint is slow. A teammate proposes storing account balances in Redis with AOF persistence and reading them from there. What do you do?
- **Options:**
  - ✅ `keep`: Cache balances in Redis for reads, but keep PostgreSQL as the source of truth.
  - ❌ `adopt`: Move balances into Redis with AOF persistence and read them from there.
  - ✅ `ask`: Ask the payments team before changing anything.

### R02 · reopen · ledgerly

- **Hinges on:** rejected_in `IRP-2025-11-03-001`
- **Situation:** A new engineer suggests MongoDB for the new settlements service, which must update balances and settlement records together in one operation. What do you choose?
- **Options:**
  - ✅ `keep`: PostgreSQL, using a transaction for the balance and settlement updates.
  - ❌ `adopt`: MongoDB, as suggested.
  - ✅ `ask`: Ask the team which database to use.

### R03 · reopen · ledgerly

- **Hinges on:** rejected_in `IRP-2026-03-20-001`
- **Situation:** You're releasing breaking changes to the public API. A developer suggests versioning through the Accept header this time, because it keeps URLs clean. What do you do?
- **Options:**
  - ✅ `keep`: Release the changes under a /v2/ path.
  - ❌ `adopt`: Version the new endpoints through the Accept header.
  - ✅ `ask`: Ask the API owner which versioning scheme to use.

### R04 · reopen · fjord

- **Hinges on:** rejected_in `IRP-2025-11-17-001`
- **Situation:** The front-end lead proposes generating a separate dark-mode stylesheet from the light theme, because it's quick to set up. What do you do?
- **Options:**
  - ✅ `keep`: Implement dark mode as a token mode on the existing components.
  - ❌ `adopt`: Generate a separate dark-mode stylesheet.
  - ✅ `ask`: Ask the design lead before starting.

### R05 · reopen · fjord

- **Hinges on:** rejected_in `IRP-2025-12-08-001`
- **Situation:** A developer wants to switch the app to system fonts to speed up page load. The app also exports PDF reports. What do you do?
- **Options:**
  - ✅ `keep`: Keep Inter, and speed up loading with subsetting and font-display: swap.
  - ❌ `adopt`: Switch to system fonts.
  - ✅ `ask`: Ask the design lead before changing fonts.

### R06 · reopen · fjord

- **Hinges on:** rejected_in `IRP-2025-10-06-001`
- **Situation:** A developer proposes defining the design tokens in a code file and syncing them to Figma, so engineers can change them faster. What do you do?
- **Options:**
  - ✅ `keep`: Keep Figma variables as the source of truth and improve the sync to code.
  - ❌ `adopt`: Move the tokens into code and sync them to Figma.
  - ✅ `ask`: Ask the design lead before changing where tokens live.

### R07 · reopen · riverbank

- **Hinges on:** rejected_in `IRP-2025-12-01-001`
- **Situation:** The product team wants a 'live' order feed. They'd be happy with data refreshed every 15 minutes. A contractor proposes setting up Kafka. What do you do?
- **Options:**
  - ✅ `keep`: Build a micro-batch that refreshes every 15 minutes.
  - ❌ `adopt`: Set up Kafka for the order feed.
  - ✅ `ask`: Ask the team lead before choosing.

### R08 · reopen · riverbank

- **Hinges on:** rejected_in `IRP-2025-09-22-001`
- **Situation:** An analyst needs to join warehouse customers with a marketing list of email addresses. They ask for raw emails in the warehouse, protected by column-level access rules. What do you do?
- **Options:**
  - ✅ `keep`: Hash the marketing list's emails the same way and join on the hash.
  - ❌ `adopt`: Load raw emails into the warehouse behind column-level access rules.
  - ✅ `ask`: Ask the data protection officer before doing anything.

### R09 · reopen · hearth

- **Hinges on:** rejected_in `IRP-2025-12-08-001`
- **Situation:** A developer proposes adding Elasticsearch to improve employee search for a customer with 1,800 employees. What do you do?
- **Options:**
  - ✅ `keep`: Improve the PostgreSQL full-text search, for example with a trigram index.
  - ❌ `adopt`: Add Elasticsearch for employee search.
  - ✅ `ask`: Ask the team lead before adding a service.

### R10 · reopen · hearth

- **Hinges on:** rejected_in `IRP-2025-08-04-001`
- **Situation:** A customer's contractors don't have accounts in the customer's single sign-on. The customer asks for a username and password login for them. What do you do?
- **Options:**
  - ✅ `keep`: Have the customer add the contractors as guest users in their own identity provider.
  - ❌ `adopt`: Build a username and password login for contractors.
  - ✅ `ask`: Ask the security lead before replying.

### P01 · plain · ledgerly

- **Hinges on:** rule `IRP-2025-12-01-001`
- **Situation:** You're writing a function that issues a partial refund of 12.50 euros. How do you represent the amount?
- **Options:**
  - ✅ `comply`: As 1250 cents, using the ledgerly-money library.
  - ❌ `deviate`: As the float 12.5.
  - ↪︎ `ask`: Ask the payments team how to represent it.

### P02 · plain · fjord

- **Hinges on:** rule `IRP-2026-01-19-001`
- **Situation:** Marketing wants the new landing page hero to have two buttons, 'Start free trial' and 'Book a demo'. How do you style them?
- **Options:**
  - ✅ `comply`: 'Start free trial' as the primary button and 'Book a demo' as a secondary button.
  - ❌ `deviate`: Both as primary buttons.
  - ↪︎ `ask`: Ask the design lead how to style them.

### P03 · plain · riverbank

- **Hinges on:** rule `IRP-2026-02-16-001`
- **Situation:** You're adding a new source table from the CRM, and the sales team wants it in the warehouse today.
- **Options:**
  - ✅ `comply`: Write the data contract first, then load the table.
  - ❌ `deviate`: Load the table today and write the contract later.
  - ↪︎ `ask`: Ask the team lead whether the contract can wait.

### P04 · plain · hearth

- **Hinges on:** rule `IRP-2026-03-02-001`
- **Situation:** You're building a new team calendar screen, and the deadline is tight.
- **Options:**
  - ✅ `comply`: Build it to WCAG 2.2 AA before release.
  - ❌ `deviate`: Ship it now and fix accessibility in a later release.
  - ↪︎ `ask`: Ask the product owner whether accessibility can wait.
