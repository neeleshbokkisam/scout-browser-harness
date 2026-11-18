# scout-browser-harness

**Personal** Exa work-trial project ([neeleshbokkisam/scout-browser-harness](https://github.com/neeleshbokkisam/scout-browser-harness)).  
Not affiliated with Cursor team work or Merged Systems.

## Goal

Keep Exa’s **Scout loop fixed** (model, prompts, budgets, research loop) and only change the **tool belt**:

1. Baseline: Scout + Exa search (browser off)
2. Treatment: Scout + Exa search **+ browser-use tools**
3. Evaluate both on **Odysseys2** and show uplift
4. Bonus: ablate browser variants and explain the intuition

## Layout

```
vendor/exa-benchmarks/     # Scout harness (exa-labs/benchmarks)
vendor/odysseys2-benchmark # Odysseys2 tasks + judge prompt
src/scout_browser/         # Our browser tools + Odysseys2 runner (Scout loop unchanged)
configs/                   # scout-search-only vs scout+browser presets
scripts/                   # setup / sync / smoke runs
results/                   # local run outputs (gitignored)
```

## Setup

```bash
cp .env.example .env
# fill OPENAI_API_KEY (Charon) + EXA_API_KEY locally — never paste into chat

python3 -m venv .venv
source .venv/bin/activate
# Scout deps (uv preferred if installed):
cd vendor/exa-benchmarks && uv sync --locked && cd ../..
pip install -e .
playwright install chromium   # when browser tools are wired
```

Charon:

```bash
export OPENAI_BASE_URL=https://charon.internal.exa.ai/v1
# model id Scout expects: openai/gpt-6-luna
```

## Remotes

- Origin: `https://github.com/neeleshbokkisam/scout-browser-harness.git` (private)
- No Merged Systems remotes
