# DepPulse — capture & publish guide (your last mile)

The engine is built, tested, and proven live. What's left are the steps that **require your
Modiqo account + an interactive OAuth login + the real `rote` binary** — an agent session
can't do these for you. Budget ~15–20 minutes.

> ⚠️ The `rote …` command flags below are **inferred** from Modiqo's docs (the GitHub repo was
> unreachable when this was written). After you install, run `rote how` and reconcile the exact
> flags. The *logic, ordering, and `@unit` boundaries* are what matter and are already correct —
> the DepPulse Python engine (`deppulse.py`) is the source of truth for behavior; `rote` just
> captures a run of it.

## 0. Prerequisites (do this first)
- Python 3.8+ on PATH (`python --version`).
- This `deppulse/` folder.
- A Modiqo/rote account for the registry you'll publish to.

## 1. Install rote & sign in (interactive — only you can do this)
```bash
curl -fsSL https://raw.githubusercontent.com/modiqo/rote-releases/main/install.sh | sh
rote how                       # confirm install; see the ~12 core commands
# complete the registry login / OAuth when prompted, and connect your agent harness
```

## 2. Smoke-test the four adapters (5 min, avoids surprises mid-capture)
```bash
rote proc   -- python deppulse.py scan --dir fixtures/node-app     # shell adapter
rote probe  GET https://status.npmjs.org/api/v2/summary.json       # API adapter
rote browse open https://health.aws.amazon.com/health/status       # browser adapter (read-only)
rote extract -- python -c "print('ok')"                            # compose adapter
```
Each should capture an indexed `@unit` with request/response/timing.

## 3. Capture DepPulse as a Play (RUN → TRACE → PLAY)
Run the guided pipeline **in order** so rote records each step as a `@unit`. This mirrors
`deppulse.py`'s subcommands exactly:

```bash
# @u1  scan (proc)
rote proc --name scan-manifests -- python deppulse.py scan --dir .            > u1.json
# @u2  map (extract)
rote extract --name map-providers -- python deppulse.py map --in u1.json      > u2.json
# @u3  probe (probe, fan-out, credential-free)
rote probe --name status-summary -- python deppulse.py probe --in u2.json     > u3.json
# @u_board  compose (extract)
rote extract --name compose-board -- python deppulse.py compose --map u2.json --status u3.json --format board
```
> Simplest alternative: capture a single `rote proc --name deppulse -- python deppulse.py run --dir .`
> if you'd rather ship one `@unit`. The four-step version above is more inspectable and shows off
> all four adapters — preferred for judging.

## 4. Prove it (the trust assets judges reward)
```bash
rote trace     # inspect captured @units (request/response/timing/deps)
rote replay    # determinism: re-run from captured fixtures -> byte-identical board
rote doctor    # risk report — EXPECT: no creds, no writes, cwd-only reads, pinned-network flag only
```
Screenshot the clean `rote doctor` output — it's your strongest trust signal.

Local determinism proof you can show without rote:
```bash
python -m unittest discover -s tests -v                            # 17 tests, all green
```

## 5. Publish = submit
```bash
rote play publish        # at the Team / Community / Skip prompt -> choose COMMUNITY
```
There is no separate form. The public link (e.g. `play.modiqo.ai/<you>/deppulse`) **is** your entry.
Then verify from a clean/incognito machine — pull and run it exactly as a stranger would.

## 6. Social + adoption (Apple Watch prize + the adoption judging axis)
- Post the sample board + link on **X and LinkedIn, tagging Modiqo**; cross-post in the WeMakeDevs Discord.
- Ask 3–5 other participants to run it in **their** repos (each stack → a different provider set, so it's instantly relevant). Each independent pull is a scored adoption signal.
- Invite provider-table PRs — contributors become long-term users.

## If you hit trouble
- `rote` flag mismatch → run `rote how`, adjust the wrappers in step 3 (behavior lives in `deppulse.py`, so only the outer `rote …` invocation changes).
- A status endpoint moved → edit `adapters/dep-providers.table.json` + `adapters/status-allowlist.txt` (keep them in sync) and re-run `python -m unittest`.
- Paste me the `rote how` output and I'll rewrite step 3 to the exact real flags.

## Descope safety (if time runs short — see docs/rote-playoffs/03-implementation-plan.md)
The JSON path (`scan → map → probe → compose`) is the non-negotiable ship-minimum and is **already
done and passing**. Everything else (browser leg, `--url`) is optional and off by default. A
reliable JSON-only DepPulse beats an ambitious broken one on every judging axis.
