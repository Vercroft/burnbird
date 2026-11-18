# Add a provider or a runner size

One pull request does all of it.

1. Add one row per runner size to `src/burnbird/billing/providers.toml`: its label patterns, its price or weight a
   minute written as a string, and the pages its facts come from with the date you read them.
2. Add each new page's host to the `url-host` rule's list of hosts this repository alone cites, in `.gitleaks.toml`.
3. Add one fixture job per new row to `FIXTURES` in `tests/billing/test_classify.py`. The coverage test fails for a
   row without one.
4. For a new runner size of a listed provider, stop here: it is data only.
5. For a new provider, add its step to the classification order in `src/burnbird/billing/classify.py`.
6. For a new rounding rule or a new kind of allowance, change `src/burnbird/billing/counting.py` or
   `src/burnbird/billing/forecast.py`, with tests.
