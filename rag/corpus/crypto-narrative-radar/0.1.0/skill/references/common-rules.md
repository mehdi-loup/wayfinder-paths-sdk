# Common rules

These apply to every agent in this pipeline. Read before doing anything else.

## What we are hunting

We are hunting **asymmetrically skewed returns**. The goal is to surface theses with very high upside — either trades that can scale to meaningful size, or trades where the payoff is a multiple of the risk if the thesis plays out. Things we don't want:

- Carry-style theses with bounded upside.
- "Modest re-rate" stories — small expected moves with symmetric downside.
- Anything where the best-case outcome is unremarkable.

If a thesis doesn't have an obvious skewed-upside story, it isn't for this pipeline. The skeptic chain decides how *likely* the upside is; your job during scanning is to find the strongest case for the upside existing at all. Bias toward boldness, not coverage.

## What "skewed upside" looks like

A thesis qualifies when at least one of these is true and you can explain why:

- The trade can absorb real size without erasing edge — the kind of setup where capacity itself is the moat.
- The payoff path implies a multiple of the entry risk if the catalyst resolves the way the thesis predicts.

These are guides, not gates. Use judgment. If you're unsure whether a thesis is "big enough," default to including it and let the adversarial chain rule on whether the case holds up.

## How to write theses

Each candidate thesis should make the upside story explicit — what the payoff path is, what unlocks it, and why the downside is contained. Don't hide the asymmetry inside generic phrasing.

## Data discipline

Adapters/clients are the primary source for any quantitative claim. See `skill/references/data-sources.md`. WebSearch is for qualitative context only.

## Adversarial chain is sacred

You are not your own skeptic. Bring the strongest version of the thesis; the novelty gate, pre-mortem, consensus auditor, and historical analogist exist to stress-test it.
