# DepPulse

When your build breaks or your app throws a 500 error, run this before you debug your own code.

DepPulse tells you in one command whether an upstream provider your repository actually depends on is having an incident right now. You don't have to open eight status pages by hand.

```text
$ python deppulse.py run --dir .

DepPulse: is it me, or is a provider down?
1 provider(s) degraded

Cloudflare    partial outage   "degraded: Baghdad (BGW), ..."
      https://www.cloudflarestatus.com  <- included because: wrangler (package.json)
Docker Hub    operational
      https://www.dockerstatus.com  <- included because: node:20-alpine (Dockerfile)
GitHub        operational
      https://www.githubstatus.com  <- included because: @octokit/rest (package.json)
npm Registry  operational
      https://status.npmjs.org  <- included because: package.json
OpenAI        operational
      https://status.openai.com  <- included because: openai (package.json)

no JSON status page (check manually):
  Stripe        https://status.stripe.com  <- included because: stripe (package.json)

Coverage: checked 5 of 6 detected providers; 1 had no JSON status page.
Scanned 3 manifest file(s), 6 dependencies. Table v1.
```

## Run it in 30 seconds

Standalone, requiring no installation besides Python 3.8+:
```bash
cd <any repo with a lockfile>        # package.json / requirements.txt / go.mod / Dockerfile
python /path/to/deppulse.py run --dir .
```

As a rote Play:
```bash
rote registry adapter pull deppulse
rote play run deppulse
```

It requires no login, no API key, and no configuration. It works on the very first run.

## Why you can trust it before running it

The credential list is completely empty. There is nothing to hand over.

It is read-only. It reads lockfiles inside your current folder and never writes, installs, or runs your project's code.

The network traffic is fully inspectable. It only contacts a small, pinned list of public status pages. You can open `adapters/dep-providers.table.json` and `adapters/status-allowlist.txt` to audit the exact URLs before you run it.

It is honest about coverage gaps. The coverage line shows what it could not check. It never pretends.

It never breaks on you. If a status page is slow or changes layout, that provider simply shows as unknown and the run still finishes.

## Flags

`--url https://api.myapp.com/healthz` 
Also ping your own endpoint, so you can see if the provider is green but your app is down. It is still credential-free.

`--providers github,npm` 
Check exactly these providers instead of auto-detecting them.

`--browser` 
Enable the best-effort read of JSON-less pages like AWS Health or Stripe. This is off by default.

`--format json` 
Produce a machine-readable board to pipe into your agent or CI.

`--no-emoji` 
Use ASCII status lights instead of emoji.

## What it does under the hood

The system runs four distinct steps. 

First, it scans. It reads your lockfiles for the dependencies you actually use. 
Second, it maps. It matches those dependencies to providers via the pinned table, recording which dependency pulled each one in.
Third, it probes. It executes a credential-free GET request against each provider's public `summary.json`. 
Finally, it composes. It merges everything into a worst-first board with a single unified verdict.

The optional browse leg reads the few pages without a JSON feed, like AWS and Stripe. It is off by default, ensuring the reliable JSON path always carries the run.

## Coverage and extending

The provider table currently covers 11 JSON-feed providers, including GitHub, npm, PyPI, Docker Hub, OpenAI, Anthropic, Cloudflare, Vercel, Netlify, Twilio, and Datadog. It also covers 2 manual-leg providers in AWS and Stripe. 

Every JSON endpoint was verified to return 200 status codes on August 24, 2026. Adding a provider is a one-entry pull request to `adapters/dep-providers.table.json`. Coverage grows as people use it.

## Trust surface at a glance

Read `skill.spec.yml` for the machine-checkable contract. It enforces deny-by-default tool boundaries, empty credential sets, empty write sets, the pinned network allowlist, and all completion-proof checks.
