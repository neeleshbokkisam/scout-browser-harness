# 15-minute story

## Thesis (30s)

Scout already measures **search quality** with a fixed agent loop. Odysseys2 tasks need **page state** (click, fill, scroll, multi-step UI). Adding browser tools to Scout’s tool belt—without changing the loop—should unlock large gains on interaction-heavy tasks while search remains the discovery engine.

## Setup (2 min)

- Fixed: model (`openai/gpt-6-luna` via Charon), Scout prompts, budgets, `submit_final_result`
- Variable: tools only
  - **A** search-only (Exa)
  - **B** search + browser
- Eval: Odysseys2 (100 tasks / 982 rubric checks), same judge recipe

## Why search alone fails here (3 min)

Pick 2–3 Odysseys2 examples (shopping compare, form/ZIP, multi-page listing):

- Search returns stale/snippet evidence; rubrics demand live page facts, ASIN order, logged-out ZIP, etc.
- Show a failed search-only trajectory vs a browser trajectory that meets rubric items

## Method (3 min)

- Loop unchanged: tool schemas added beside `search` + `submit_final_result`
- Browser primitives (minimal belt): navigate / snapshot / click / type / extract
- Fairness: same model, same budgets; report cost + wall time

## Results (4 min)

- Headlines: mean rubric score A vs B, perfect-pass rate, category breakdown
- Confidence: bootstrap / paired task deltas
- Cost–quality: when browser steps are worth it

## Ablations + intuition (2 min)

| Variant | Intuition |
|---------|-----------|
| Search only | Cheap discovery; weak on interactive state |
| Browser only | Can act, but slow / myopic without search |
| Search → then browse | Search finds entry points; browser verifies/extracts |
| Full concurrent tool belt | Model chooses; measure over/under-use |

## Close (30s)

Browser isn’t a new agent—it’s the missing tool for Odysseys2’s interaction surface. Scout stays the fair harness; tool belt explains the uplift.
