"""
Daily collector for the CPI food / fruit & vegetables basket.

Modes
-----
discover : list stores and files per chain, dump raw listing material to data/discover/
catalog  : download PriceFull (+PromoFull) for the selected store of each chain and write the
           complete product list to data/catalog/<chain>_<YYYY-MM-DD>.csv.gz
daily    : download PriceFull + PromoFull for the selected stores, extract the basket
           (collector/basket.csv), append observations to data/prices/<YYYY-MM>.csv,
           refresh data/latest.json and data/coverage.csv

All timestamps are Asia/Jerusalem dates.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import (  # noqa: E402
    make_adapters,
    decode_payload,
    parse_prices,
    parse_promos,
    promo_open_to_all,
    promo_unit_price,
    promo_active,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CFG = json.loads((ROOT / "collector" / "config.json").read_text(encoding="utf-8"))

IL_TZ = timezone(timedelta(hours=3))  # IDT; the date is what matters, not the hour


def today_il():
    return datetime.now(IL_TZ).date()


import signal


class ChainTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise ChainTimeout("chain step exceeded time budget")


def with_budget(seconds, fn, *a, **k):
    """Run fn with a hard wall-clock budget (Linux only)."""
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(seconds))
    try:
        return fn(*a, **k)
    finally:
        signal.alarm(0)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- helpers
def pick_latest(files: list[dict], store_id=None) -> dict | None:
    """Latest file (by the timestamp embedded in the file name) for a store."""
    cand = [f for f in files if store_id is None or f.get("store_id") == store_id]
    if not cand:
        return None

    def key(f):
        import re

        m = re.search(r"-(\d{8})-?(\d{4,6})?", f["name"])
        return (m.group(1) + (m.group(2) or "")) if m else f["name"]

    return sorted(cand, key=key)[-1]


def download_kind(ad, kind: str, store_id: int) -> tuple[dict | None, list[dict]]:
    files = ad.list_files(kind, store_id=store_id) if kind != "Stores" else ad.list_files(kind)
    f = pick_latest(files, store_id if kind != "Stores" else None)
    if f is None:
        log(f"  {ad.chain}: no {kind} file for store {store_id}")
        return None, []
    raw = ad.fetch(f["url"])
    xml = decode_payload(raw)
    rows = parse_prices(xml) if kind.startswith("Price") else parse_promos(xml)
    log(f"  {ad.chain}: {kind} {f['name']} -> {len(rows)} rows")
    return f, rows


def effective_prices(prices: list[dict], promos: list[dict], on) -> pd.DataFrame:
    """Join shelf prices with the best open-to-all promotion valid on `on`."""
    dfp = pd.DataFrame(prices)
    if dfp.empty:
        return dfp
    dfp = dfp.drop_duplicates("barcode", keep="last")
    best = {}
    for p in promos:
        if not promo_active(p, on) or not promo_open_to_all(p):
            continue
        best.setdefault(p["barcode"], []).append(p)
    shelf = dict(zip(dfp.barcode, dfp.price))
    promo_price, promo_desc, promo_id, promo_minqty = {}, {}, {}, {}
    for bc, lst in best.items():
        sp = shelf.get(bc)
        cands = []
        for p in lst:
            up = promo_unit_price(p, sp)
            if up is None or up <= 0:
                continue
            if sp and up > sp:  # not a discount for this item
                continue
            cands.append((up, p))
        if cands:
            up, p = min(cands, key=lambda t: t[0])
            promo_price[bc], promo_desc[bc], promo_id[bc], promo_minqty[bc] = up, p["promo_desc"], p["promo_id"], p["min_qty"]
    dfp["promo_price"] = dfp.barcode.map(promo_price)
    dfp["promo_desc"] = dfp.barcode.map(promo_desc)
    dfp["promo_id"] = dfp.barcode.map(promo_id)
    dfp["promo_min_qty"] = dfp.barcode.map(promo_minqty)
    dfp["effective_price"] = dfp[["price", "promo_price"]].min(axis=1)
    return dfp


# --------------------------------------------------------------------------- modes
def mode_discover(ads):
    out = DATA / "discover"
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, ad in ads.items():
        cfg = next(c for c in CFG["chains"] if c["chain"] == name)
        if cfg.get("store_id") is not None:
            continue
        log(f"discover {name}")
        try:
            if name == "carrefour":
                info = ad.discover()
                (out / "carrefour_index.html").write_text(info.pop("index_html", ""), encoding="utf-8")
                (out / "carrefour_endpoints.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            if hasattr(ad, "discover") and name != "carrefour":
                dbg = with_budget(300, ad.discover)
                (out / f"{name}_discover.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            files = with_budget(600, ad.list_files, "PriceFull")
            pd.DataFrame(files).to_csv(out / f"{name}_pricefull_files.csv", index=False, encoding="utf-8-sig")
            summary[name] = {"n_pricefull_files": len(files), "sample": files[:5]}
        except Exception as e:  # noqa
            summary[name] = {"error": str(e), "trace": traceback.format_exc()[-2000:]}
            log(f"  FAILED: {e}")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def mode_catalog(ads, on):
    out = DATA / "catalog"
    out.mkdir(parents=True, exist_ok=True)
    for ch in CFG["chains"]:
        name, store = ch["chain"], ch.get("store_id")
        if store is None:
            log(f"{name}: no store_id configured, skipping")
            continue
        ad = ads[name]
        try:
            fp, prices = with_budget(600, download_kind, ad, "PriceFull", store)
            _, promos = with_budget(600, download_kind, ad, "PromoFull", store)
            df = effective_prices(prices, promos, on)
            df.insert(0, "chain", name)
            df.insert(1, "store_id", store)
            df.insert(2, "date", on.isoformat())
            df.insert(3, "source_file", fp["name"] if fp else "")
            with gzip.open(out / f"{name}_{on.isoformat()}.csv.gz", "wt", encoding="utf-8", newline="") as fh:
                df.to_csv(fh, index=False)
            log(f"{name}: catalog {len(df)} items written")
        except Exception as e:  # noqa
            log(f"{name}: catalog FAILED {e}")
            traceback.print_exc()


def mode_daily(ads, on):
    basket = pd.read_csv(ROOT / "collector" / "basket.csv", dtype={"barcode": str, "cbs_code": str})
    basket["barcode"] = basket["barcode"].str.strip()
    obs_all = []
    status = {"date": on.isoformat(), "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "chains": {}}
    for ch in CFG["chains"]:
        name, store = ch["chain"], ch.get("store_id")
        if store is None:
            continue
        ad = ads[name]
        bk = basket[basket.chain == name]
        try:
            fp, prices = with_budget(600, download_kind, ad, "PriceFull", store)
            pf, promos = with_budget(600, download_kind, ad, "PromoFull", store)
            df = effective_prices(prices, promos, on)
            m = bk.merge(df, on="barcode", how="left", suffixes=("", "_file"))
            m.insert(0, "date", on.isoformat())
            m["store_id"] = store
            m["source_file"] = fp["name"] if fp else ""
            m["promo_file"] = pf["name"] if pf else ""
            m["found"] = m["price"].notna().astype(int)
            obs_all.append(m)
            status["chains"][name] = {
                "ok": True,
                "price_file": fp["name"] if fp else None,
                "promo_file": pf["name"] if pf else None,
                "basket_items": int(len(bk)),
                "found": int(m["found"].sum()),
                "on_promo": int(m["promo_price"].notna().sum()),
            }
        except Exception as e:  # noqa
            log(f"{name}: daily FAILED {e}")
            traceback.print_exc()
            status["chains"][name] = {"ok": False, "error": str(e)}
    if not obs_all:
        raise SystemExit("no chain succeeded")
    obs = pd.concat(obs_all, ignore_index=True)
    cols = [
        "date", "chain", "store_id", "barcode", "cbs_code", "cbs_name", "group", "product_name", "name",
        "manufacturer", "quantity", "unit_of_measure", "unit_qty", "is_weighted", "price", "unit_price",
        "promo_price", "promo_min_qty", "promo_desc", "promo_id", "effective_price", "price_update", "found",
        "source_file", "promo_file",
    ]
    for c in cols:
        if c not in obs.columns:
            obs[c] = None
    obs = obs[cols]
    pdir = DATA / "prices"
    pdir.mkdir(parents=True, exist_ok=True)
    fn = pdir / f"{on.strftime('%Y-%m')}.csv"
    if fn.exists():
        old = pd.read_csv(fn, dtype={"barcode": str, "cbs_code": str})
        old = old[old["date"] != on.isoformat()]  # idempotent re-run for the same day
        obs = pd.concat([old, obs], ignore_index=True)
    obs.to_csv(fn, index=False, encoding="utf-8")
    (DATA / "latest.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    cov = (
        obs[obs["date"] == on.isoformat()]
        .groupby(["group", "cbs_code", "cbs_name", "chain"], as_index=False)
        .agg(items=("barcode", "size"), found=("found", "sum"), on_promo=("promo_price", lambda s: int(s.notna().sum())))
    )
    cov.to_csv(DATA / "coverage.csv", index=False, encoding="utf-8-sig")
    log(f"daily: {len(obs[obs['date']==on.isoformat()])} observations written to {fn.name}")
    log(json.dumps(status, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, help="discover|catalog|daily, comma-separated for several")
    ap.add_argument("--date", help="override observation date YYYY-MM-DD")
    a = ap.parse_args()
    on = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else today_il()
    ads = make_adapters(CFG)
    for mode in [m.strip() for m in a.mode.split(",") if m.strip()]:
        log(f"=== mode {mode} ===")
        if mode == "discover":
            mode_discover(ads)
        elif mode == "catalog":
            mode_catalog(ads, on)
        elif mode == "daily":
            mode_daily(ads, on)
        else:
            raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
