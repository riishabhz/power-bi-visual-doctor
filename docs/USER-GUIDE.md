# pbi-visual-doctor user guide

This guide takes you from a fresh machine to a health report for every Power BI
report you can open, with root causes worked out locally by the Laya model.

- [1. What it does](#1-what-it-does)
- [2. Install](#2-install)
- [3. Sign in once](#3-sign-in-once)
- [4. Scan](#4-scan)
- [5. Read the report](#5-read-the-report)
- [6. Triage with Laya](#6-triage-with-laya)
- [7. Improving accuracy](#7-improving-accuracy)
- [8. Other backends](#8-other-backends)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Privacy and security](#10-privacy-and-security)

## 1. What it does

```
pbi-doctor login   ->  you sign in once in a browser window
pbi-doctor scan    ->  finds every report and page you can access (Power BI REST API)
                   ->  opens each page in a hidden browser and reads every visual as text
                   ->  flags visuals that show an error, stay blank or never finish loading
                   ->  sends only those to a triage model (Laya, Jev or offline rules)
                   ->  writes one HTML health report with root cause, owner and severity
```

Everything runs on your machine. No screenshots are taken, and healthy visuals
never go to a model.

## 2. Install

You need Python 3.10 or newer.

```bash
git clone https://github.com/riishabhz/pbi-visual-doctor
cd pbi-visual-doctor
pip install -e .
playwright install chromium
```

Try it without a Power BI account first:

```bash
pbi-doctor demo
```

Open `pbi-doctor-demo/report.html`.

**"pbi-doctor is not recognized"** (common with the Microsoft Store Python on
Windows): use `python -m pbi_visual_doctor` in place of `pbi-doctor` everywhere
in this guide, for example `python -m pbi_visual_doctor scan`.

## 3. Sign in once

```bash
pbi-doctor login
```

A browser window opens. Sign in to Power BI, including MFA. When the Power BI
home page loads, the session is saved to `.auth/state.json` and the window closes
by itself.

Sessions expire after a while (often days to a couple of weeks, depending on your
organization). When a scan says your session has expired, run `login` again.

## 4. Scan

```bash
pbi-doctor scan
```

With no options this scans every report in every shared workspace you can access.

| Option | What it does |
| --- | --- |
| `--include-my-workspace` | Also scan reports in My workspace. **Needed if your reports live there.** |
| `--workspace "Sales"` | Only this workspace: full name, part of the name, or id. Repeatable. |
| `--report "Pipeline"` | Only reports whose name contains this text. Repeatable. |
| `--url <link>` | A report or workspace link copied from the browser. Repeatable. |
| `--include-hidden-pages` | Also scan hidden pages (drill-through and tooltip pages). They are labelled "hidden page". |
| `--list` | Print what would be scanned and stop. Nothing is opened. |
| `--headed` | Show the browser while it scans. |
| `--concurrency 4` | Pages scanned in parallel (default 3). |
| `--backend laya` | Which triage model to use. See section 6. |
| `--fail-on-broken` | Exit with code 1 when anything is broken (for scheduled runs). |

A good first run is a dry run:

```bash
pbi-doctor scan --include-my-workspace --list
```

```
Found 4 report(s), 18 page(s):
  My workspace / Sales Overview  2 page(s)
  My workspace / Getting Started in Power BI  9 page(s)
  ...
```

Hidden pages are skipped by default, because drill-through and tooltip pages
expect a filter from another page and often look empty on their own.

## 5. Read the report

Every scan gets its own folder, named after the time and backend, so earlier
results are kept:

```
pbi-doctor-report/
  2026-09-26_22-28-34_laya/
    report.html     the health report
    findings.json   every problem visual with the model's answers
    findings.csv    the same, flat; load it into Power BI or Excel
    scan.json       the raw crawl
```

`report.html` has:

- **Totals**: reports, pages, visuals checked, broken visuals, items that need human review.
- **Top fixes**: identical failures grouped together. A renamed measure that breaks 23
  visuals across 8 reports is one row, not 23.
- **By root cause** and **by owner**.
- **By report**: every page with its status and an **open page** link straight to it in
  Power BI. Low-confidence answers are marked "review".

What counts as a problem visual:

| Status | Meaning |
| --- | --- |
| `error` | The visual shows an error, for example "Can't display this visual" or any message with a **See details** link |
| `blank` | "(Blank)", "No data", or a titled visual that drew nothing. The model decides whether it is really broken |
| `timeout` | Still loading after 45 seconds |

Text boxes, images, shapes and buttons are never checked.

## 6. Triage with Laya

[Laya](https://github.com/NandhaKishorM/laya) is an open-weights model (Apache 2.0)
that answers typed questions (pick one option, give a score) instead of writing
text. It runs on your own CPU or GPU, needs no API key, and nothing leaves your
machine.

### Install

```bash
pip install -e ".[laya]"
```

This pulls in PyTorch and Transformers, a few GB in total. Laya is optional for
that reason.

### Run

```bash
pbi-doctor scan --include-my-workspace --backend laya
```

The first run downloads the model (about 800 MB) from Hugging Face into
`~/.cache/huggingface`. Later runs reuse it and work offline. To keep the model
somewhere else, for example next to the project, set `HF_HOME` first:

```powershell
$env:HF_HOME = "D:\models\huggingface"
```

You may see this warning; it comes from the model files and does not stop the run:

```
RuntimeWarning: laya: this checkpoint ships invalid temperatures ...
Treat confidence from the affected entries as uncalibrated.
```

### Re-triage without crawling again

Crawling is the slow part. To try another backend, or new category wording, on a
scan you already have:

```bash
pbi-doctor triage --input pbi-doctor-report/<run-folder>/scan.json --backend laya
```

This writes a new `..._laya_triage` folder and leaves the original run alone.
Note that triage reuses the ok / problem status saved at scan time; after changing
error phrases in the config, run a new scan instead.

### What Laya is asked

For each problem visual Laya gets the report, page, visual title and type, the
on-canvas message and the "See details" text, and answers:

| Question | Answer |
| --- | --- |
| Root cause | missing field, DAX error, relationship, permissions, credentials or gateway, refresh failure, resource limit, custom visual, tenant setting, not broken, other |
| Owner | report author, semantic model owner, platform admin |
| Severity | cosmetic, partial, unusable visual, critical |
| Is it broken? (blank visuals only) | a probability |

Each answer has a confidence. Anything below 0.6 is marked for human review
(change it with `triage.review_threshold` in a config file).

## 7. Improving accuracy

Laya picks the option whose description best matches the error text, so how the
options are worded matters a lot. They live in
`src/pbi_visual_doctor/triage.py` (`CATEGORIES`, `OWNERS`).

What worked in testing:

- **Word each option the way Power BI's own messages read.** Describing "missing
  field" as "the message says a field ... doesn't exist, cannot be found" instead of
  an abstract definition raised accuracy on our test set from 5 of 8 to 8 of 8.
- **Give every common error its own option.** "Map and filled map visuals aren't
  enabled for your org" was labelled "custom visual" at 29 to 35% confidence until a
  `tenant_setting` option existed; then it was 97%.
- **Re-test after every change.** One new option can pull other errors toward it.
  Use `pbi-doctor triage` on a saved scan to compare quickly.

Known weak spots on the English checkpoint: the owner answer has low confidence,
and "Can't determine relationships between the fields" is not recognised as a
relationship problem. Check items marked "review".

Beyond wording, the larger step is fine-tuning Laya on your own labelled errors,
using Laya's own tooling. `findings.csv` from each run is a starting point for
that dataset.

**Detection** (which visuals count as problems) is separate from triage and is
configured in `src/pbi_visual_doctor/defaults.yaml` under `phrases`. Override any
part with your own file:

```yaml
# my-config.yaml
phrases:
  error: ["Can't display this visual", "See details", "Your own message"]
timing:
  render_max_wait_ms: 60000
triage:
  review_threshold: 0.7
```

```bash
pbi-doctor scan --config my-config.yaml --backend laya
```

## 8. Other backends

| Backend | `--backend` | Needs | Notes |
| --- | --- | --- | --- |
| Rules | `rules` (default) | nothing | Keyword matching. Good for trying the tool; not a model. |
| Laya, local | `laya` | `pip install -e ".[laya]"` | Free, offline, private. |
| Laya, server | `laya-http` | a running `laya-serve` | Share one Laya instance across machines. `LAYA_BASE_URL` (default `http://localhost:8000`). |
| Jev | `jev` | `TYPESAFE_API_KEY` | Hosted by TypeSafe, stronger zero-shot. The error text of problem visuals leaves your machine. |

Without a key, `--backend jev` stops with "Set TYPESAFE_API_KEY to use the Jev backend."

## 9. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Error: You are not signed in` | Run `pbi-doctor login`. |
| `Your saved Power BI session has expired` | Run `pbi-doctor login` again. |
| `No reports to scan` | Your reports are probably in My workspace: add `--include-my-workspace`. Check with `--list`. |
| `No workspace matches 'X'` | The message lists the workspaces you can see; use one of those names. |
| A page shows 0 visuals | The page may hold only text boxes and images, which are skipped. If a page with charts shows 0, Power BI's markup may have changed: run with `--headed` and check `selectors` in `defaults.yaml`. |
| A broken visual was not reported | Its page may be hidden (use `--include-hidden-pages`), or its message is not in `phrases.error`. Add it with `--config`. |
| Scan is slow | Each page waits for its visuals to settle. Raise `--concurrency` if your capacity copes. |

## 10. Privacy and security

- `.auth/state.json` is your signed-in browser session. **Keep it private**; anyone
  with it can open Power BI as you. It is in `.gitignore`.
- To list workspaces, reports and pages, the tool reads the access token the
  Power BI web app already holds. The token is kept in memory only and never
  written to disk. Set `PBI_ACCESS_TOKEN` to supply your own instead.
- With `rules`, `laya` or `laya-http`, nothing leaves your machine except the normal
  traffic to Power BI.
- With `jev`, only problem visuals are sent: report and page name, visual title and
  type, status and the error text (up to 1,500 characters). Error text can contain
  table, column and data source names.
- Reports are scanned with their default filters. A visual that renders a wrong
  number without an error is not detected.
