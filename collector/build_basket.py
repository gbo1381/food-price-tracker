"""
Build collector/basket.csv from the catalog snapshots in data/catalog/.

Method
------
* Packaged food (CBS sub-items with barcodes): pick barcodes that appear in as many of the
  four chains as possible, classify each to a CBS sub-item by keyword rules on the product name,
  prefer national brands, and keep the 2-3 most-shared, most-typical barcodes per sub-item PER CHAIN
  (the same barcode in every chain where it exists, so the item is a like-for-like cross-chain fixed
  basket). Weighted (loose) products are excluded here.
* Fresh fruit & vegetables (weighted): matched by name keywords against the per-kg weighted items in
  each chain (barcodes differ across chains), one representative weighted product per CBS produce line.

Output columns: chain, group, cbs_code, cbs_name, product_name, barcode, brand, kind(packaged|weighted), match
"""
from __future__ import annotations
import glob, re, json
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATS = {}
for f in glob.glob(str(ROOT / "data/catalog/*.csv.gz")):
    d = pd.read_csv(f, dtype={"barcode": str})
    CATS[d["chain"].iloc[0]] = d
CHAINS = list(CATS.keys())

NAT_BRANDS = ["אסם","תנובה","שטראוס","עלית","תלמה","יטבתה","טרה","טבעול","עלית","קוקה","פפסי","נסטלה",
              "מטרנה","סימילק","ויסוצקי","וויסוצקי","סנו","אנג'ל","ברמן","מאפיית","פרימור","פריניר",
              "יכין","מולר","גד","מילקה","קלאב","קרלסברג","גולדסטאר","מכבי","טובורג","פריגת","זוגלובק",
              "של","מעדנות","עמק","גבינת","סוגת","וילי","דנונה","יופלה"]

# (group, cbs_code, cbs_name, include-regex, exclude-regex). First matching rule wins.
GLOBAL_EXCL = r"פרווה|טבעוני|תחליף|צמחי|לא ידוע$"
RULES = [
    ("food",120070,"לחם", r"\bלחם |לחמנ|פיתות|פיתה|באגט|\bחלה\b", r"פירורי|קיסמי|לחמניית|תבלין"),
    ("food",120080,"ביסקוויטים, עוגות וכדומה", r"ביסקוויט|עוגיו|וופל|קרקר|\bבמבה\b|ביסלי|קרואסון|מציות|פריכיות", r"מרק|במבה נוגט תרסיס"),
    ("food",120090,"קמח", r"^קמח |\bקמח (חיטה|לבן|מלא|תופח|כוסמין)", r"פיצה|פוקצה|שמרים"),
    ("food",120100,"מוצרי בצק", r"פסטה|ספגטי|ספגטיני|אטריות|פנה|פוסילי|פתיתים|קוסקוס|מקרוני|לזניה|רביולי|נוקי", r"טרייה|ביצים"),
    ("food",120110,"קטניות ומוצרי דגן", r"^אורז|\bאורז (לבן|מלא|בסמטי|עגול)|עדשים|גריסים|בורגול|קינואה|קורנפלקס|דגני בוקר|גרנולה|שיבולת שועל", r""),
    ("food",120140,"בשר בקר", r"בשר בקר|בקר טחון|טחון בקר|אנטריקוט|שייטל|אסאדו|צלי כתף|בשר טחון", r"קרטון|בקרוב"),
    ("food",120150,"בשר בהמה אחר: כבש וחזיר", r"כבש טחון|בשר כבש|צלעות כבש|צלעות טלה|בשר טלה|שריר טלה|בשר חזיר|צלע חזיר", r"גבינ|פטה|קשקבל|גביע|בירה|נוטלה|סטלה"),
    ("food",120160,"עופות", r"חזה עוף|שניצל עוף|כרעי עוף|שוקיים|כנפיים|פרגית|בשר הודו|שוקי עוף|עוף טרי|עוף שלם", r"מרק|טבעול|מאמא|תבלין|קוביות"),
    ("food",120170,"בשר משומר ומעובד", r"נקניקי|\bנקניק\b|קבב|פסטרמה|סלמי|\bלוף\b|שווארמה", r"טבעוני|טבעול|מוצרלה|גבינ"),
    ("food",120190,"שימורי דגים ודגים מעובדים", r"טונה|סרדינ|הרינג|פילה מלוח|דג מלוח|מטיאס", r""),
    ("food",120180,"דגים", r"פילה סלמון|פרוסות סלמון|אמנון|מושט שלם|דניס|בקלה|לברק|נסיכת הנילוס|פילה דג|דג טרי", r"פרידג|ברנפלקס|קולה|חתול|כלב|דנטלייף|פיסט|פנסי|מעדן חתול|לחיות"),
    ("food",120210,"שמנים", r"^שמן |שמן זית|שמן קנולה|שמן חמניות|שמן סויה|שמן חמנייה", r"לאבנה|גבינ|בשמן זית"),
    ("food",120220,"מרגרינה", r"מרגרינה", r""),
    ("food",120240,"חלב", r"^חלב |\bחלב (טרי|תנובה|טרה|3%|1%|בקרטון|בשקית|מהדרין)", r"שוקולד|מילקה|מעדן|משקה|קפה|וניל|עמיד|סויה|שקדים|שיבולת"),
    ("food",120250,"לבן, יוגורט ומעדני חלב", r"יוגורט|\bלבן\b|אשל|מעדן חלב|דנונה|יופלה|שמנת חמוצה|\bלבנה\b|גיל \d", r"שוקולד|מילקה|בסגנון"),
    ("food",120260,"שמנת", r"שמנת מתוקה|שמנת להקצפה|שמנת חמוצה|^שמנת ", r"גבינ|נפוליאון"),
    ("food",120270,"חמאה", r"^חמאה |\bחמאה \d|חמאת בקר", r"וילי|דבש|בוטנים|שום|פופקו"),
    ("food",120280,"גבינה", r"גבינה צהובה|קוטג|מוצרלה|\bפטה\b|בולגרית|\bעמק\b|גאודה|גבינה לבנה|גבינת שמנת|צ'דר|קשקבל", r"נקניק|שלגון|טעמקור|גלידה"),
    ("food",120285,"גלידות", r"גלידה|ארטיק|שלגון|קרטיב|מקופלת גלידה", r"קרמיסימו פרווה"),
    ("food",120120,"ביצים", r"^\d+ ביצים|ביצים (גדולות|L|M|XL|טריות|חופש)|תבנית ביצים", r"פסטה|ברילה|אטריות"),
    ("food",120380,"סוכר ותחליפיו", r"^סוכר |\bסוכר (לבן|חום|דמררה)|סוכרזית|ממתיק|סוכר גולדן", r"קטשופ|מופחת|20%"),
    ("food",120390,"ריבה, דבש וכדומה", r"^ריבת|\bריבה\b|^דבש |\bדבש \d|דבש טהור|סילאן|ממרח פירות", r"עוגת|תה|נסטלה|פיטנס"),
    ("food",120400,"ממתקים ושוקולד", r"שוקולד (חלב|מריר|לבן|פרה)|טבלת שוקולד|ממתק|מסטיק|קליק|שוקו פרה|במבה נוגט", r"משקה|שוקולית|גלידה|עוגת|קרמיסימו"),
    ("food",120350,"משקאות קלים", r"קוקה קולה|פפסי|ספרייט|פאנטה|\bסודה\b|מיץ ענבים|נקטר|שוופס|מים מינרל|מים בטעמים|XL |פיוז טי", r"בירה"),
    ("food",120360,"משקאות אלכוהולים", r"\bבירה\b|\bיין\b|וודקה|וויסקי|ויסקי|ערק|ליקר|קוניאק|טקילה|ג'ין |\bרום\b|ברנדי|קרלסברג|היינקן|קורונה", r"0%|נטול|ללא אלכוהול"),
    ("food",120300,"תבלינים, רטבים, מוצרים לעוגה ומזון לתינוקות", r"קטשופ|מיונז|רוטב סויה|רוטב עגבניות|\bתבלין|מלח שולחן|חרדל|טחינה גולמית|חומץ|אבקת אפיה|שמרים יבש|פודינג|מזון תינוק|מחית פירות|וניל תמצית", r"מופחת סוכר"),
    ("food",120310,"תה", r"^תה |\bתה (ירוק|שחור|צמחים|לימונענע|נענע|מנטה|קמומיל)|תיון|חליטת", r""),
    ("food",120320,"קפה", r"^קפה |נס קפה|קפה נמס|אספרסו|קפה טורקי|קפסולות קפה|פולי קפה|ספלנדיד.*קפה|עלית קפה", r"שוקולד"),
    ("food",120330,"קקאו", r"שוקו |משקה שוקו|אבקת קקאו|שוקולית|קקאו ממתק", r"גלידה|קרמיסימו"),
    ("food",150315,"סלטים מוכנים", r"מטבוחה|סלט חצילים|חומוס מוכן|סלט טחינה|ממרח סלט|סלט קטן|סלטי", r""),
]

PRODUCE = [  # (cbs_code, product, name-keywords)
    (120042,"עגבניות", r"עגבניות(?! שרי)"),
    (120042,"מלפפונים", r"מלפפון"),
    (120042,"בצל יבש", r"בצל יבש|בצל לבן|\bבצל\b"),
    (120042,"תפוחי אדמה", r"תפוח אדמה|תפוחי אדמה|תפו\"א"),
    (120042,"גזר", r"\bגזר\b"),
    (120042,"פלפל", r"פלפל אדום|פלפל|גמבה"),
    (120043,"בננות", r"בננה|בננות"),
    (120043,"תפוחים", r"תפוח עץ|תפוחים|תפוח סמיט|תפוח פינק"),
    (120043,"תפוזים", r"תפוז|תפוזים"),
    (120043,"ענבים", r"ענבים|ענבי"),
    (120043,"אבטיח", r"אבטיח"),
    (120043,"מלון", r"\bמלון\b"),
]


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def is_national(brand, name):
    t = f"{brand} {name}"
    return any(b in t for b in NAT_BRANDS)


def build_packaged():
    # union of barcodes with per-chain presence; keep the LONGEST name across chains (least truncated)
    presence = {}
    names = {}
    brands = {}
    for ch, df in CATS.items():
        d = df[df["is_weighted"].astype(str) != "1"]
        for _, r in d.iterrows():
            b = r["barcode"]
            if not isinstance(b, str) or len(b) < 6:
                continue
            presence.setdefault(b, set()).add(ch)
            nm = norm(r["name"])
            if len(nm) > len(names.get(b, "")):
                names[b] = nm
            br = norm(r.get("manufacturer", ""))
            if br and br != "לא ידוע" and b not in brands:
                brands[b] = br
    meta = {b: (names[b], brands.get(b, "")) for b in names}
    # classify with include + exclude
    classified = {}
    for b, (name, brand) in meta.items():
        if re.search(GLOBAL_EXCL, name):
            continue
        for group, code, cbs, pat, excl in RULES:
            if re.search(pat, name) and not (excl and re.search(excl, name)):
                classified.setdefault((group, code, cbs), []).append((b, name, brand, len(presence[b])))
                break
    basket = []
    for (group, code, cbs), lst in classified.items():
        # prefer present-in-most-chains, then national brand, then shorter (more generic) name
        lst.sort(key=lambda t: (-t[3], not is_national(t[2], t[1]), len(t[1])))
        chosen = [t for t in lst if t[3] >= 3][:3] or [t for t in lst if t[3] >= 2][:2]
        if not chosen:
            continue
        for b, name, brand, npres in chosen:
            for ch in CHAINS:
                if ch in presence[b]:
                    basket.append(dict(chain=ch, group=group, cbs_code=code, cbs_name=cbs,
                                       product_name=name, barcode=b, brand=brand,
                                       kind="packaged", match=f"shared/{npres}"))
    return pd.DataFrame(basket)


def build_produce():
    basket = []
    for ch, df in CATS.items():
        d = df[df["is_weighted"].astype(str) == "1"].copy()
        d["nm"] = d["name"].map(norm)
        EXC = r"יבש|מיובש|קפוא|משומר|כבוש|חתוך|פרוס|ממרח|רסק|סלט|ארוז|במיכל|קלוי|מגי"
        for code, prod, pat in PRODUCE:
            cand = d[d["nm"].str.contains(pat, regex=True) & ~d["nm"].str.contains(EXC, regex=True)]
            if cand.empty:
                cand = d[d["nm"].str.contains(pat, regex=True)]
            if cand.empty:
                continue
            cand = cand.assign(L=cand["nm"].str.len()).sort_values("L")
            r = cand.iloc[0]
            grp = "fv"
            basket.append(dict(chain=ch, group=grp, cbs_code=code, cbs_name={120042:"ירקות טריים",120043:"פירות טריים"}[code],
                               product_name=f"{prod}: {r['nm']}", barcode=r["barcode"], brand="",
                               kind="weighted", match="weighted-name"))
    return pd.DataFrame(basket)


if __name__ == "__main__":
    pk = build_packaged()
    pr = build_produce()
    b = pd.concat([pk, pr], ignore_index=True)
    b = b.sort_values(["group", "cbs_code", "chain", "product_name"]).reset_index(drop=True)
    b.to_csv(ROOT / "collector" / "basket.csv", index=False, encoding="utf-8-sig")
    # coverage report
    cov = b.groupby(["group","cbs_code","cbs_name"]).agg(
        chains=("chain","nunique"), products=("barcode","nunique"), rows=("barcode","size")).reset_index()
    print(cov.to_string())
    print("\nsub-items with <4 chains or 0 products:")
    print(cov[(cov.chains<4)].to_string())
    print("\nTOTAL rows",len(b),"packaged",len(pk),"produce",len(pr))
