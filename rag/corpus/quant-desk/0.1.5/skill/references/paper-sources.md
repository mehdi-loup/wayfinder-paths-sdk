# Paper sources

## Accepted (academic only) — pre-approved domains in `.claude/settings.local.json`

These `WebFetch(domain:...)` permissions are allowlisted so Phase 1/2 agents
don't block on permission prompts:

- `arxiv.org` — q-fin, stat.AP sections (primary)
- `papers.ssrn.com`, `ssrn.com` — SSRN working papers
- `www.nber.org`, `nber.org` — NBER working papers
- `onlinelibrary.wiley.com` — Wiley journals (JoF, Mathematical Finance, etc.)
- `www.sciencedirect.com` — Elsevier (JFE)
- `academic.oup.com` — Oxford (RFS, Review of Finance)
- `link.springer.com` — Springer journals
- `www.tandfonline.com` — Taylor & Francis
- `www.cambridge.org` — Cambridge (JFQA)
- `www.aeaweb.org` — AEA journals (AER, JEP)
- `www.jstor.org` — JSTOR archives
- `www.bis.org` — BIS working papers
- `www.federalreserve.gov` — Fed working paper series
- `www.ecb.europa.eu` — ECB working papers
- `www.bankofengland.co.uk` — BoE
- `www.imf.org` — IMF working papers
- `doi.org` — DOI redirects (resolves to publisher)
- `scholar.google.com` — Google Scholar search
- `www.semanticscholar.org`, `semanticscholar.org` — Semantic Scholar
- `ideas.repec.org`, `econpapers.repec.org` — RePEc aggregators

## Rejected

- Medium, Substack, LinkedIn
- Seeking Alpha, Bloomberg opinion, CNBC
- Twitter / X threads (even with "paper-like" claims)
- Practitioner blogs (AQR, Two Sigma, etc. blog posts — **their published papers ARE accepted** if found on SSRN/arxiv)
- Reddit (r/quant, r/algotrading)
- Substack newsletter "research"

## Search heuristics

- Query arxiv first (`https://arxiv.org/search/?searchtype=all&query=...`) — fast, structured, abstract-first
- Then SSRN (Google query: `site:papers.ssrn.com <topic>`)
- Then NBER (`https://www.nber.org/search?searchString=...`)

## Quality bias

Within accepted sources, prefer:
- Recent (2015+) work reflecting realistic costs and modern methods
- Papers cited 50+ times (Google Scholar) — signal of follow-up validation
- Replication or meta-study papers ("does X factor still work?") — often pre-digested skepticism

De-prioritize:
- Very old (pre-2010) unless the canonical reference on a topic
- Conference-only papers without peer review
- Working papers with no revisions in 2+ years (may be discontinued work)
