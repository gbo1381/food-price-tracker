"""
Build the daily food / fruit-veg price indices and the tracking workbook from data/prices/*.csv.

Index construction (mirrors the CBS elementary-aggregate logic)
--------------------------------------------------------------
1. Product level: effective price (min of shelf and open-to-all promotion) per barcode/chain/day.
   For each product we form a price RELATIVE to its own price on the basket's base day (first day it
   is seen), so a product that enters late does not create a jump.
2. Sub-item (CBS code) level, per chain: geometric mean (Jevons) of the product relatives.
3. Sub-item level, across chains: geometric mean of the chain sub-indices (equal chain weight).
4. Basket level: weight each sub-item by its CBS 2025 weight and take a weighted geometric mean
   -> food index (ex fruit&veg) and fruit&veg index. Both are chained from true day-on-day moves and
   rebased to 100 on the base day.

The workbook has: בסיס נתונים (raw daily obs), מדד יומי (daily index levels + d/d %), השוואה חודשית
(month averages vs the CPI item), כיסוי (coverage), הסל (basket), מתודולוגיה.
"""
from __future__ import annotations
import glob
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "workbook"
OUT.mkdir(parents=True, exist_ok=True)

WEIGHTS = pd.read_csv(ROOT / "collector" / "weights.csv")
W = dict(zip(WEIGHTS.cbs_code.astype(str), WEIGHTS.weight_permille))
NAMES = dict(zip(WEIGHTS.cbs_code.astype(str), WEIGHTS.cbs_name))


def load_prices() -> pd.DataFrame:
    fs = sorted(glob.glob(str(DATA / "prices" / "*.csv")))
    if not fs:
        return pd.DataFrame()
    df = pd.concat([pd.read_csv(f, dtype={"barcode": str, "cbs_code": str}) for f in fs], ignore_index=True)
    df = df[df["found"] == 1].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["eff"] = pd.to_numeric(df["effective_price"], errors="coerce")
    df = df[df["eff"] > 0]
    return df


def geomean(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x) & (x > 0)]
    return float(np.exp(np.mean(np.log(x)))) if len(x) else np.nan


def build_indices(df: pd.DataFrame):
    """Return (daily_index, subitem_daily) dataframes."""
    dates = sorted(df["date"].unique())
    base = dates[0]
    # base price per product (chain+barcode): first observed effective price
    key = ["chain", "barcode"]
    first = df.sort_values("date").groupby(key).first().reset_index()[key + ["eff"]].rename(columns={"eff": "p0"})
    d = df.merge(first, on=key, how="left")
    d["rel"] = d["eff"] / d["p0"]
    # sub-item per chain per day (Jevons over products)
    g = d.groupby(["date", "group", "cbs_code", "chain"]).agg(rel=("rel", geomean), n=("barcode", "nunique")).reset_index()
    # across chains (equal weight)
    gc = g.groupby(["date", "group", "cbs_code"]).agg(rel=("rel", geomean), chains=("chain", "nunique"), n=("n", "sum")).reset_index()
    gc["cbs_name"] = gc["cbs_code"].map(NAMES)
    gc["w"] = gc["cbs_code"].map(W)
    # basket level per group per day (weighted geometric mean of sub-item relatives)
    rows = []
    for (dt, grp), gg in gc.groupby(["date", "group"]):
        gg = gg.dropna(subset=["rel", "w"])
        if gg.empty:
            continue
        wsum = gg["w"].sum()
        idx = float(np.exp(np.sum(gg["w"] * np.log(gg["rel"])) / wsum)) * 100
        rows.append({"date": dt, "group": grp, "index": idx, "sub_items": len(gg), "coverage_permille": wsum})
    daily = pd.DataFrame(rows).sort_values(["group", "date"])
    daily["dod_pct"] = daily.groupby("group")["index"].pct_change() * 100
    daily["cum_pct"] = (daily["index"] / 100 - 1) * 100
    return daily, gc, base


def monthly_compare(daily: pd.DataFrame):
    d = daily.copy()
    d["ym"] = d["date"].dt.strftime("%Y-%m")
    m = d.groupby(["group", "ym"]).agg(index_avg=("index", "mean"), days=("date", "nunique")).reset_index()
    m["mom_pct"] = m.groupby("group")["index_avg"].pct_change() * 100
    return m


def build_workbook(df, daily, sub, base):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils.dataframe import dataframe_to_rows

    wb = Workbook()
    hdr = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="2F5597")
    gname = {"food": "מזון (ללא פו\"ר)", "fv": "פירות וירקות"}

    def sheet(title, dfin, note=None, freeze="A2"):
        ws = wb.create_sheet(title)
        r0 = 1
        if note:
            ws.cell(1, 1, note).font = Font(italic=True, color="555555")
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(2, len(dfin.columns)))
            r0 = 2
        for j, c in enumerate(dfin.columns, 1):
            cell = ws.cell(r0, j, str(c)); cell.font = hdr; cell.fill = fill; cell.alignment = Alignment(horizontal="center")
        for i, row in enumerate(dataframe_to_rows(dfin, index=False, header=False), r0 + 1):
            for j, v in enumerate(row, 1):
                if isinstance(v, float) and np.isnan(v):
                    v = None
                ws.cell(i, j, v)
        ws.freeze_panes = ws.cell(r0 + 1, 1)
        ws.sheet_view.rightToLeft = True
        for col in ws.columns:
            L = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max(L + 2, 10), 46)
        return ws

    wb.remove(wb.active)

    # מדד יומי
    dwide = daily.pivot(index="date", columns="group", values="index").reset_index()
    dwide.columns = ["תאריך"] + [gname.get(c, c) for c in dwide.columns[1:]]
    dd = daily.pivot(index="date", columns="group", values="dod_pct").reset_index()
    for c in dd.columns[1:]:
        dwide[f"{gname.get(c,c)} d/d %"] = dd[c].values
    dwide["תאריך"] = pd.to_datetime(dwide["תאריך"]).dt.strftime("%Y-%m-%d")
    sheet("מדד יומי", dwide.round(3),
          note=f"מדד יומי (בסיס 100 = {pd.to_datetime(base).strftime('%d/%m/%Y')}). d/d = שינוי יומי באחוזים. מקור: קבצי שקיפות המחירים, מחיר בפועל כולל מבצע פתוח לכל.")

    # השוואה חודשית
    m = monthly_compare(daily)
    m["group"] = m["group"].map(gname)
    mw = m.pivot(index="ym", columns="group", values="mom_pct").reset_index().rename(columns={"ym": "חודש"})
    sheet("השוואה חודשית", mw.round(2),
          note="שינוי חודשי ממוצע במדד היומי (%). כאשר מצטבר חודש מלא — זו האינדיקציה למדד המחירים לצרכן של אותו חודש.")

    # מדד לפי תת-סעיף (יומי, אחרון)
    last = sub[sub.date == sub.date.max()].copy()
    last["שינוי מהבסיס %"] = (last["rel"] - 1) * 100
    last = last[["group", "cbs_code", "cbs_name", "w", "chains", "n", "שינוי מהבסיס %"]]
    last.columns = ["קבוצה", "קוד", "תת-סעיף", "משקל ‰", "רשתות", "מוצרים", "שינוי מהבסיס %"]
    last["קבוצה"] = last["קבוצה"].map(gname)
    sheet("תת-סעיפים", last.round(2), note=f"מצב ליום {pd.to_datetime(sub.date.max()).strftime('%d/%m/%Y')}: שינוי מצטבר מהבסיס לכל תת-סעיף.")

    # כיסוי
    cov = (df[df.date == df.date.max()].groupby(["group", "cbs_code", "cbs_name", "chain"])
           .agg(מוצרים=("barcode", "nunique"), במבצע=("promo_price", lambda s: int(pd.to_numeric(s, errors="coerce").notna().sum()))).reset_index())
    cov.columns = ["קבוצה", "קוד", "תת-סעיף", "רשת", "מוצרים", "במבצע"]
    sheet("כיסוי", cov, note="מספר מוצרים שנמצאו וכמה מהם במבצע פתוח לכל, לפי רשת ותת-סעיף, ליום האחרון.")

    # בסיס נתונים
    raw = df[["date", "chain", "group", "cbs_code", "cbs_name", "product_name", "barcode",
              "price", "promo_price", "effective_price"]].copy()
    raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
    raw.columns = ["תאריך", "רשת", "קבוצה", "קוד", "תת-סעיף", "מוצר", "ברקוד", "מחיר מדף", "מחיר מבצע", "מחיר בפועל"]
    sheet("בסיס נתונים", raw, note="כל התצפיות היומיות. מחיר בפועל = מינימום בין מדף למבצע פתוח לכל.")

    # הסל
    basket = pd.read_csv(ROOT / "collector" / "basket.csv", dtype={"barcode": str})
    basket = basket[["group", "cbs_code", "cbs_name", "product_name", "barcode", "brand", "chain", "kind"]]
    basket.columns = ["קבוצה", "קוד", "תת-סעיף", "מוצר", "ברקוד", "מותג", "רשת", "סוג"]
    sheet("הסל", basket, note="הרכב הסל: מוצרים קבועים לכל תת-סעיף (ברקוד משותף לרשתות במוצרים ארוזים; פריט שקיל בפו\"ר).")

    # מתודולוגיה
    ws = wb.create_sheet("מתודולוגיה"); ws.sheet_view.rightToLeft = True
    meth = [
        "מעקב מחירי מזון יומי — אינדיקציה מקדימה למדד המחירים לצרכן",
        "",
        "מקור: קבצי שקיפות המחירים היומיים (חוק שקיפות המחירים) של שופרסל, רמי לוי, טיב טעם וקרפור — סניף קבוע לכל רשת.",
        "תדירות: יומית. הורדה אוטומטית ב-07:40 שעון ישראל דרך GitHub Actions.",
        "מחיר: מחיר בפועל = מינימום בין מחיר המדף למבצע פתוח לכל צרכן (ללא מועדון, קופון, מינימום קנייה או מעל 3 יח') — כפי שהלמ\"ס רושמת.",
        "",
        "הסל: לכל תת-סעיף בסעיף המזון של המדד נבחרו 2-3 ברקודים של מותגים ארוזים המשותפים לרשתות; פירות וירקות טריים נדגמים כפריטים שקילים.",
        "משקולות: משקולות הלמ\"ס 2025 (מתוך 1000), מקובץ 115 הפריטים.",
        "",
        "שיטת המדד (במתכונת אגרגט אלמנטרי של הלמ\"ס):",
        "1. לכל מוצר — יחס מחיר מול מחירו ביום הבסיס (היום הראשון בו נצפה), כך שכניסת מוצר חדש אינה יוצרת קפיצה.",
        "2. תת-סעיף לכל רשת — ממוצע גאומטרי (Jevons) של יחסי המוצרים.",
        "3. תת-סעיף בין רשתות — ממוצע גאומטרי (משקל שווה לרשתות).",
        "4. הסל — ממוצע גאומטרי משוקלל במשקולות הלמ\"ס. מדד המזון אינו כולל פו\"ר; מדד פו\"ר בנפרד.",
        "",
        "פרשנות: השינוי החודשי הממוצע במדד היומי (גיליון 'השוואה חודשית') הוא האינדיקציה למדד המזון/פו\"ר של אותו חודש.",
        "מגבלות: הסל עוקב אחר קבוצה קבועה של מוצרים — אות ה-m/m תקף גם אם רמת המחיר אינה זהה לרמת הלמ\"ס. ארוחות בחוץ (31.7‰) ובשר כבש/חזיר אינם נדגמים.",
        "פו\"ר: קיים כבר מודל נאוקאסט על מחירי משרד החקלאות השבועיים (מתאם 0.91); הדגימה היומית משלימה אותו ותיבחן מולו לאחר כמה חודשי חפיפה.",
    ]
    for i, line in enumerate(meth, 1):
        c = ws.cell(i, 1, line)
        if i == 1:
            c.font = Font(bold=True, size=13)
        elif line.endswith(":") or line.startswith(("מקור", "תדירות", "מחיר", "הסל", "משקולות", "פרשנות", "מגבלות", "פו")):
            c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 140

    # order sheets
    order = ["מדד יומי", "השוואה חודשית", "תת-סעיפים", "כיסוי", "בסיס נתונים", "הסל", "מתודולוגיה"]
    wb._sheets.sort(key=lambda s: order.index(s.title) if s.title in order else 99)
    fn = OUT / "מעקב מחירי מזון.xlsx"
    wb.save(fn)
    return fn


if __name__ == "__main__":
    df = load_prices()
    if df.empty:
        print("no price data yet"); raise SystemExit
    daily, sub, base = build_indices(df)
    fn = build_workbook(df, daily, sub, base)
    daily.to_csv(DATA / "index_daily.csv", index=False, encoding="utf-8-sig")
    print("days:", df["date"].nunique(), "| latest index:")
    print(daily[daily.date == daily.date.max()][["group", "index", "dod_pct", "sub_items", "coverage_permille"]].to_string())
    print("workbook ->", fn)
