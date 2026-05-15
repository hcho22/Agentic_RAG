<!-- BEGIN EVAL_SUMMARY -->

### Headline (mean across 50 questions)

| Mode | recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| vector | 0.670 | 0.655 | 0.629 |
| keyword | 0.110 | 0.120 | 0.112 |
| hybrid | 0.670 | 0.645 | 0.621 |

### Per-category breakdown

| Mode | Category | recall@5 | MRR |
|---|---|---|---|
| vector | single_chunk | 0.750 | 0.700 |
| vector | multi_hop | 0.767 | 0.850 |
| vector | adversarial | 0.400 | 0.300 |
| vector | paraphrase | 0.600 | 0.600 |
| keyword | single_chunk | 0.250 | 0.250 |
| keyword | multi_hop | 0.033 | 0.067 |
| keyword | adversarial | 0.000 | 0.000 |
| keyword | paraphrase | 0.000 | 0.000 |
| hybrid | single_chunk | 0.750 | 0.700 |
| hybrid | multi_hop | 0.767 | 0.850 |
| hybrid | adversarial | 0.400 | 0.250 |
| hybrid | paraphrase | 0.600 | 0.600 |

### Security (US-042) — fraction of no_access runs that returned 0 gold chunks

| Mode | Pre-filter | Post-filter |
|---|---|---|
| vector | 1.000 | 1.000 |
| keyword | 1.000 | 1.000 |
| hybrid | 1.000 | 1.000 |

### Recall trade-off (US-042) — partial_access recall@5: pre-filter vs post-filter

| Mode | Category | Pre | Post | Δ (pre−post) |
|---|---|---|---|---|
| vector | overall | 0.670 | 0.670 | +0.000 |
| vector | single_chunk | 0.750 | 0.750 | +0.000 |
| vector | multi_hop | 0.767 | 0.767 | +0.000 |
| vector | adversarial | 0.400 | 0.400 | +0.000 |
| vector | paraphrase | 0.600 | 0.600 | +0.000 |
| keyword | overall | 0.110 | 0.110 | +0.000 |
| keyword | single_chunk | 0.250 | 0.250 | +0.000 |
| keyword | multi_hop | 0.033 | 0.033 | +0.000 |
| keyword | adversarial | 0.000 | 0.000 | +0.000 |
| keyword | paraphrase | 0.000 | 0.000 | +0.000 |
| hybrid | overall | 0.670 | 0.670 | +0.000 |
| hybrid | single_chunk | 0.750 | 0.750 | +0.000 |
| hybrid | multi_hop | 0.767 | 0.767 | +0.000 |
| hybrid | adversarial | 0.400 | 0.400 | +0.000 |
| hybrid | paraphrase | 0.600 | 0.600 | +0.000 |

### Non-regression (US-042) — full_access recall@5 vs Module-10 baseline

| Mode | Actual | Baseline | Δ | Within ±0.005? |
|---|---|---|---|---|
| vector | 0.670 | 0.670 | +0.000 | ✓ |
| keyword | 0.110 | 0.110 | +0.000 | ✓ |
| hybrid | 0.670 | 0.670 | +0.000 | ✓ |

<!-- END EVAL_SUMMARY -->
