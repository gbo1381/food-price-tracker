# food-price-tracker

Daily sampling of a CPI-representative food basket and a fruit & vegetables basket from the
Israeli price-transparency files (Shufersal, Rami Levy, Tiv Taam, Carrefour), for nowcasting the
food and F&V items of the Israeli CPI.

* `collector/` – collector code, chain config, basket definition (`basket.csv`), CBS weights.
* `data/prices/YYYY-MM.csv` – daily basket observations (shelf price, best open-to-all promotion, effective price).
* `data/catalog/` – full product lists of the sampled stores (for basket maintenance).
* `data/discover/` – raw listing material from the discovery run.

Runs: GitHub Actions, daily 04:40 UTC; push-triggered runs use `collector/run_mode.txt`.
