# Signal contract

Every implemented signal must match this Python contract:

```python
import pandas as pd

def signal_fn(
    prices: pd.DataFrame,      # index: pd.DatetimeIndex (hourly UTC), columns: symbols
    funding: pd.DataFrame,     # same index, columns: symbols (per-hour funding rate)
    lending: dict | None,      # optional, lending["supply"][symbol] = DataFrame[ts × venue]
) -> pd.DataFrame:
    """Return a signal value per (timestamp, symbol).

    Must return a DataFrame with:
      - same index as `prices` (hourly UTC)
      - columns = prices.columns (one column per symbol)
      - float values (NaN allowed during warm-up)
    Must not use future data.
    """
```

## Constraints

- **No look-ahead:** at time `t`, use only data `<= t`. Use `.shift(1)` if unsure, and verify on a lag test.
- **No external data:** only `(prices, funding, lending)`. No internet calls, no file reads.
- **Deterministic:** same inputs produce same output.
- **Return shape matches `prices`:** if a signal is scalar-per-timestamp (e.g. BTC/ETH correlation), broadcast it across all columns.
- **Warm-up periods:** emit `NaN` during warm-up. Do not fill.

## Naming + location

- File path: `$WAYFINDER_SCRATCH_DIR/paper_replication/<topic-slug>/signals/<paper-slug>.py`
- Function name: `signal_fn`
- Top-of-file docstring: name, authors, year, arxiv/SSRN ID, brief spec restatement, known deviations (if any)

## Underspecification

If the paper doesn't give you a specific parameter value, do not guess. Create the file with this header and do not implement:

```python
"""
STATUS: UNDERSPECIFIED
Missing: [list of parameters not reported in paper]
Reference: [paper id]
"""
```

The replicator harness will skip files marked `UNDERSPECIFIED`.

## Example stub

```python
"""
Volatility-managed portfolios
Moreira, Muir 2017, JoF
Spec: scale exposure by 1/σ² where σ is rolling realized volatility.
"""

import numpy as np
import pandas as pd

WINDOW_H = 30 * 24  # paper uses monthly; rescaled to 30d hourly
TARGET_VOL = 0.20   # paper calibration (annual)

def signal_fn(prices, funding, lending):
    r = prices.pct_change()
    realized = r.rolling(WINDOW_H).std() * np.sqrt(8760)  # annualize
    scale = TARGET_VOL / realized.clip(lower=0.01)
    return scale.clip(upper=3.0)  # cap leverage at 3x as paper does
```
