# DepPulse

Run it before you debug your own code, when a build just went red or your app is throwing a 500. Run it during an incident retro to check whether it was ever really your fault. Or wire it into CI so the answer is already waiting the next time you ask.

DepPulse tells you in one command whether an upstream provider your repository actually depends on is having an incident right now. You don't have to open eight status pages by hand.

```text
$ python deppulse.py run --dir . --no-emoji

DepPulse - is it me, or is a provider down?
[Y] 1 provider(s) degraded

[O] Cloudflare    partial outage   "Incorrect geo location for some Cloudflare WARP users"
      https://www.cloudflarestatus.com  <- included because: wrangler (package.json)
[G] Docker Hub    operational
      https://www.dockerstatus.com  <- included because: node:20-alpine (Dockerfile)
[G] GitHub        operational
      https://www.githubstatus.com  <- included because: @octokit/rest (package.json)
[G] npm Registry  operational
      https://status.npmjs.org  <- included because: package.json
[G] OpenAI        operational
      https://status.openai.com  <- included because: openai (package.json)
[G] Stripe        operational
      https://status.stripe.com  <- included because: stripe (package.json)

Coverage: checked 6 of 6 detected providers; 0 had no JSON status page.
Scanned 3 manifest file(s), 6 dependencies. Table v3.
```

That is a real run against `fixtures/node-app` on September 5, 2026, not a mock. Exit code 0 means every checked provider is green; exit code 1 means at least one is in a problem state, so it drops straight into a CI step.

## Why "just check the status page" isn't enough

A provider's own status page is the thing you'd check by hand anyway, and it isn't always right. Two things this exact tool found while it was being built:

Cloudflare's page once read `partial outage` over "HTTP/3 issue affecting R2 custom domains" — but the component that actually escalated the row wasn't R2, or anything global. Of Cloudflare's 478 components, the one in `partial_outage` was a single city PoP: Arica, Chile. The component that matters worldwide, "Cloudflare Sites and Services", was only `degraded_performance` at the time. `--verify` exists to catch a status page stuck on green; this is the mirror problem worth naming — a status page's own severity can be noisier than the thing you actually depend on.

Anthropic's page once said `minor` while 4 of its 6 listed components — `claude.ai`, the Claude API, Claude Code, Claude Cowork — were independently reporting `partial_outage`. The one-line indicator at the top of a status page is a summary a human wrote; the per-component list underneath it is closer to the truth, and DepPulse reads the components, not just the headline.

Both are why this reads full component lists instead of one indicator field, and why `--verify` exists at all: a status page is evidence, not a fact.

## Run it in 30 seconds

Standalone, requiring no installation besides Python 3.8+:
```bash
cd <any repo with a lockfile>        # package.json / requirements.txt / go.mod / Dockerfile
python /path/to/deppulse.py run --dir .
```

As a rote Play (needs the [rote CLI](https://play.modiqo.ai), Linux/macOS/WSL):
```bash
rote play run https://play.modiqo.ai/deppulse/deppulse dir=/absolute/path/to/your/repo
```

`dir` is required. Play steps run in their own workspace, not your shell's current directory, so pass the real path to the repo you want checked (`dir=$(pwd)` from inside it works).

It requires no login, no API key, and no configuration. It works on the very first run.

## Use it in CI

Copy [`examples/github-actions.yml`](examples/github-actions.yml) into `.github/workflows/`. It clones this repo, runs DepPulse against your checked-out code before your real build steps, and never blocks the build either way (`continue-on-error: true`): a real provider outage shows as a warning triangle in the Actions log, not a red X, so it's visible without becoming a false gate. This is meant to run every time, quietly, and only matter on the day it matters.

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

`--format json` 
Produce a machine-readable board to pipe into your agent or CI.

`--no-emoji` 
Use ASCII status lights instead of emoji.

`--verify` 
Also GET each provider's real API host (not its status page) and compare. Checks both directions a status page can be wrong in: `(!)` when it says operational but the real endpoint didn't respond (the page may be stale), and `(i)` when it reports a problem but the real endpoint answered fine (the incident may be narrower than the page suggests — this is the same shape of gap the Cloudflare example above shows). Neither note ever changes a severity or the verdict. Adds 12 hosts to the ones DepPulse may contact (listed below); off by default, so a plain run never touches them.

## What it does under the hood

The system runs four distinct steps. 

First, it scans. It reads your lockfiles for the dependencies you actually use. 
Second, it maps. It matches those dependencies to providers via the pinned table, recording which dependency pulled each one in.
Third, it probes. It executes a credential-free GET request against each provider's public `summary.json`. 
Finally, it composes. It merges everything into a worst-first board with a single unified verdict.

Every provider goes through that same JSON path. AWS and Stripe used to be the exceptions, listed as "check manually" because they do not publish an Atlassian Statuspage feed. They publish their own JSON instead, so each one gets a dedicated parser: `status.stripe.com/current` for Stripe, and `health.aws.amazon.com/public/currentevents` for AWS. There is no browser leg and no scraping.

AWS events are regional, so the region is printed with the event. An "Increased Error Rates" event in UAE is not your problem if you deploy in us-east-1, and the board says so rather than making you guess.

## Coverage and extending

Try here: https://play.modiqo.ai/deppulse/deppulse

The provider table covers 13 providers, all of them checked live: GitHub, npm, PyPI, Docker Hub, OpenAI, Anthropic, Cloudflare, Vercel, Netlify, Twilio, Datadog, Stripe, and AWS. Nothing is left as "check manually". 

All 13 endpoints were verified live on September 5, 2026, including the two non-Statuspage feeds (AWS, Stripe). Adding a provider is a one-entry pull request to `adapters/dep-providers.table.json`. Coverage grows as people use it.

Twelve of those 13 also carry a `live_endpoint`: a real API host used only by `--verify`, checked against whatever the status page reported (green or a problem — "unknown" is skipped, since that's an allowlist miss or fetch error, not a real verdict either way). AWS has no single canonical global endpoint, so it's excluded from `--verify` and its status-feed check is unchanged.

## Trust surface at a glance

Read `skill.spec.yml` for the machine-checkable contract. It enforces deny-by-default tool boundaries, empty credential sets, empty write sets, the pinned network allowlist, and all completion-proof checks.

By default DepPulse contacts only the 13 status-page hosts in `adapters/status-allowlist.txt`. Passing `--verify` adds 12 more — the real API host for every provider except AWS — and only ever contacts one for a provider whose status page actually gave a verdict, never for one it couldn't check at all. Open that file and read it; it's short, and it's the complete list either way.
