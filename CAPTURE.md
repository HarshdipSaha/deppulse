# DepPulse — capture & publish guide (your last mile)

The engine is built, tested, and proven live. What's left are the steps that **require your
Modiqo account + an interactive OAuth login + the real `rote`/`play` tooling** — an agent
session can't do these for you. Budget ~15–20 minutes.

> ✅ Corrected 2026-09-03 against the real `modiqo/rote-releases` README and the official
> hackathon setup guide (modiqo.ai/blog/the-playoffs). The previous version of this file guessed
> at `rote proc`/`rote probe`/`rote extract`/`rote play publish` because GitHub was unreachable
> when it was written — **those commands don't exist.** The real mechanism is conversational:
> you don't wrap each step in a `rote` verb, you drive a natural-language `/play` session and
> Play observes what your agent does.

## 0. Prerequisites (do this first)
- Python 3.8+ on PATH (`python --version`).
- This `deppulse/` folder.
- A Modiqo account (Google or GitHub sign-in — this is your public "jersey"/handle).

## 1. Install rote & complete the six-step warmup (interactive — only you can do this)
```bash
curl -fsSL https://raw.githubusercontent.com/modiqo/rote-releases/main/install.sh | bash
# or the hackathon-pinned entrypoint:
curl -fsSL https://getrote.dev/playoffs/install.sh | sh
rote setup          # one command: registry login, adapter picks, agent wiring (<60s)
```
Then, per the official warmup checklist:
1. Confirm install: Play installed, harness detected, ready to sign in.
2. In Claude Code, type `/play what's new` to confirm the harness is wired (Codex/Cursor use `$play what's new`).
3. Sign in (Google or GitHub) to claim your public handle.
4. Run `/play what's new` again to browse public Plays.
5. Run `/play run hello` — a public-data, no-credential sample Play — to see a run end to end.
6. Do one throwaway practice Play on any small repetitive task, just to feel the flow (choose **Skip** at the storage prompt so it doesn't pollute your submission).

## 2. Capture DepPulse as your real Play
There is no per-subcommand wrapping. Inside Claude Code, in this `deppulse/` folder, say:
```
/play run the DepPulse dependency-status check on this repo, scanning manifests,
mapping dependencies to status-page providers, probing each provider's status, and
printing the worst-first board
```
Then actually guide the agent through a real run — e.g. have it execute:
```bash
python deppulse.py run --dir .
```
(or the four discrete subcommands `scan` → `map` → `probe` → `compose` if you want the
capture to show more inspectable steps — `deppulse.py`'s subcommands already mirror this).
Play watches the tool calls made during the session and turns the proven run into a
reusable, versioned Play. Correct the agent if it does anything extraneous — only the
real, useful path should get captured.

When Play judges the run reusable, it prompts **Team / Community / Skip**. Choose
**Community** — that prompt *is* the submission.

## 3. Prove it (the trust assets judges reward)
```bash
rote trace                 # Terminal Gantt chart of what the captured Play actually did
rote trace --html report.html   # shareable HTML version
```
Run `rote how` and `rote guidance` after install to see if `doctor`/`replay`-equivalent
risk/determinism reports exist under different names in your installed version — reconcile
before relying on them; they weren't confirmed in the current public docs.

Local determinism proof you can show without rote regardless:
```bash
python -m unittest discover -s tests -v                            # 17 tests, all green
```

## 4. Publish = submit
Choosing **Community** in step 2 *is* publishing — there's no separate `rote play publish`
command and no separate form. The public link (e.g. `play.modiqo.ai/<you>/deppulse`) **is**
your entry. Then verify from a clean/incognito machine, or ask someone else to pull it —
run it exactly as a stranger would.

## 5. Social + adoption (Apple Watch prize + the adoption judging axis)
- Post the sample board + link on **X and LinkedIn, tagging Modiqo**; cross-post in the WeMakeDevs Discord.
- Ask 3–5 other participants to run it in **their** repos (each stack → a different provider set, so it's instantly relevant). Each independent pull is a scored adoption signal.
- Invite provider-table PRs — contributors become long-term users.

## If you hit trouble
- `/play` behaves unexpectedly → run `rote how` / `rote guidance agent essential` to reconcile against the installed version (behavior lives in `deppulse.py`, so only how you narrate the `/play` session changes).
- A status endpoint moved → edit `adapters/dep-providers.table.json` + `adapters/status-allowlist.txt` (keep them in sync) and re-run `python -m unittest`.
- Paste me the `rote how` output and I'll refine step 2's phrasing to whatever the installed CLI actually expects.

## Descope safety (if time runs short — see docs/rote-playoffs/03-implementation-plan.md)
The JSON path (`scan → map → probe → compose`) is the non-negotiable ship-minimum and is **already
done and passing**. Everything else (browser leg, `--url`) is optional and off by default. A
reliable JSON-only DepPulse beats an ambitious broken one on every judging axis.
