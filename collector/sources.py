"""
Chain adapters for the Israeli price-transparency files (חוק שקיפות המחירים).

Each adapter exposes:
    list_files(kind)  -> list of dicts {name, url, store_id, store_name, size, updated}
                         kind in {"PriceFull", "PromoFull", "Stores"}
    fetch(url)        -> bytes (raw file, may be gz / zip / xml)

Parsing helpers (parse_prices / parse_promos) are chain-agnostic: all chains use the
same government XML schema with small case/field variations.
"""
from __future__ import annotations

import gzip
import io
import json
import re
import zipfile
from datetime import datetime, date
from typing import Iterable

import requests
from lxml import etree

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) food-price-tracker/1.0 (research; CPI nowcast)"}
TIMEOUT = 90


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


# --------------------------------------------------------------------------- decoding
def decode_payload(raw: bytes) -> bytes:
    """Return XML bytes from gz / zip / plain payload."""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            return z.read(names[0])
    return raw


def _txt(el, *names, default=None):
    """Case-insensitive child text lookup."""
    if el is None:
        return default
    lower = {c.tag.lower(): c for c in el if isinstance(c.tag, str)}
    for n in names:
        c = lower.get(n.lower())
        if c is not None and c.text is not None:
            return c.text.strip()
    return default


def _iter(el, name) -> Iterable:
    name = name.lower()
    for c in el.iter():
        if isinstance(c.tag, str) and c.tag.lower() == name:
            yield c


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_prices(xml_bytes: bytes) -> list[dict]:
    root = etree.fromstring(xml_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    out = []
    for it in _iter(root, "Item"):
        code = _txt(it, "ItemCode")
        if not code:
            continue
        out.append(
            {
                "barcode": code.strip(),
                "name": _txt(it, "ItemName", "ItemNm", default=""),
                "manufacturer": _txt(it, "ManufacturerName", "ManufactureName", default=""),
                "manuf_desc": _txt(it, "ManufacturerItemDescription", default=""),
                "unit_qty": _txt(it, "UnitQty", default=""),
                "quantity": _f(_txt(it, "Quantity")),
                "unit_of_measure": _txt(it, "UnitOfMeasure", default=""),
                "is_weighted": _txt(it, "bIsWeighted", "BisWeighted", default="0"),
                "qty_in_package": _txt(it, "QtyInPackage", default=""),
                "price": _f(_txt(it, "ItemPrice")),
                "unit_price": _f(_txt(it, "UnitOfMeasurePrice")),
                "allow_discount": _txt(it, "AllowDiscount", default=""),
                "status": _txt(it, "ItemStatus", "ItemType", default=""),
                "price_update": _txt(it, "PriceUpdateDate", default=""),
            }
        )
    return out


def _parse_dt(s: str | None):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 2] if "T" in fmt else s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def parse_promos(xml_bytes: bytes) -> list[dict]:
    """Flatten promotions to one row per (promotion, item)."""
    root = etree.fromstring(xml_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    rows = []
    for pr in _iter(root, "Promotion"):
        clubs = [c.text.strip() for c in _iter(pr, "ClubId") if c.text]
        add = None
        for a in _iter(pr, "AdditionalRestrictions"):
            add = a
            break
        base = {
            "promo_id": _txt(pr, "PromotionId"),
            "promo_desc": _txt(pr, "PromotionDescription", default=""),
            "start": _txt(pr, "PromotionStartDate"),
            "end": _txt(pr, "PromotionEndDate"),
            "reward_type": _txt(pr, "RewardType"),
            "min_qty": _f(_txt(pr, "MinQty"), 1.0) or 1.0,
            "max_qty": _f(_txt(pr, "MaxQty")),
            "discount_rate": _f(_txt(pr, "DiscountRate")),
            "discount_type": _txt(pr, "DiscountType"),
            "min_purchase": _f(_txt(pr, "MinPurchaseAmnt"), 0.0) or 0.0,
            "discounted_price": _f(_txt(pr, "DiscountedPrice")),
            "discounted_price_per_mida": _f(_txt(pr, "DiscountedPricePerMida")),
            "min_items_offered": _f(_txt(pr, "MinNoOfItemOfered")),
            "clubs": ",".join(clubs),
            "is_coupon": _txt(add, "AdditionalIsCoupon", default="0"),
            "is_total": _txt(add, "AdditionalIsTotal", default="0"),
            "gift_count": _txt(add, "AdditionalGiftCount", default="0"),
            "min_basket": _f(_txt(add, "AdditionalMinBasketAmount"), 0.0) or 0.0,
        }
        for it in _iter(pr, "Item"):
            code = _txt(it, "ItemCode")
            if not code:
                continue
            r = dict(base)
            r["barcode"] = code.strip()
            r["is_gift"] = _txt(it, "IsGiftItem", default="0")
            rows.append(r)
    return rows


def promo_open_to_all(p: dict) -> bool:
    """CBS-style rule: promotion any consumer gets at the till, no club/coupon/basket condition."""
    clubs = [c for c in p["clubs"].split(",") if c]
    if any(c not in ("0", "") for c in clubs):
        return False
    if p["is_coupon"] not in ("0", "", None):
        return False
    if p["is_total"] not in ("0", "", None):
        return False
    if (p["min_purchase"] or 0) > 0 or (p["min_basket"] or 0) > 0:
        return False
    if p["is_gift"] not in ("0", "", None):
        return False
    if p["min_qty"] and p["min_qty"] > 3:
        return False
    return True


def promo_unit_price(p: dict, shelf: float | None) -> float | None:
    """Per-unit price implied by a promotion. Returns None if it cannot be derived."""
    q = p["min_qty"] or 1.0
    dp = p["discounted_price"]
    rate = p["discount_rate"]
    dtype = (p["discount_type"] or "").strip()
    # percentage discount
    if dp is None and rate is not None and shelf:
        if dtype == "1" or rate <= 100:
            return round(shelf * (1 - rate / 100.0), 4) if rate <= 100 else None
        return None
    if dp is None:
        return None
    if q <= 1:
        return dp
    # q>1: files are inconsistent about whether DiscountedPrice is the total for q units
    # or the unit price. Disambiguate against the shelf price when available.
    if shelf:
        if dp / q <= shelf < dp * 1.05:  # total for q units
            return dp / q
        if dp <= shelf:  # already a unit price
            return dp
        return dp / q
    return dp / q


def promo_active(p: dict, on: date) -> bool:
    s, e = _parse_dt(p["start"]), _parse_dt(p["end"])
    if s and s.date() > on:
        return False
    if e and e.date() < on:
        return False
    return True


# --------------------------------------------------------------------------- Shufersal
class Shufersal:
    """prices.shufersal.co.il — HTML listing, files on Azure blob storage."""

    chain = "shufersal"
    BASE = "https://prices.shufersal.co.il"
    CAT = {"All": 0, "Price": 1, "PriceFull": 2, "Promo": 3, "PromoFull": 4, "Stores": 5}

    def __init__(self):
        self.s = _session()

    def list_files(self, kind: str, store_id: int = 0, max_pages: int = 60) -> list[dict]:
        out, page = [], 1
        while page <= max_pages:
            url = f"{self.BASE}/FileObject/UpdateCategory?catID={self.CAT[kind]}&storeId={store_id}&page={page}"
            r = self.s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, flags=re.S | re.I)
            found = 0
            for row in rows:
                m = re.search(r'href="(https?://[^"]+\.(?:gz|xml|zip)[^"]*)"', row, flags=re.I)
                if not m:
                    continue
                cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
                href = m.group(1).replace("&amp;", "&")
                fname = href.split("/")[-1].split("?")[0]
                mm = re.match(r"(Price|PriceFull|Promo|PromoFull|Stores)(\d+)-(\d+)-(\d+)?-?(\d{8})-(\d{4,6})", fname)
                store = int(mm.group(4)) if mm and mm.group(4) else None
                out.append(
                    {
                        "name": fname,
                        "url": href,
                        "store_id": store,
                        "store_name": next((c for c in cells if "-" in c and any("֐" <= ch <= "ת" for ch in c)), ""),
                        "size": next((c for c in cells if re.search(r"\d", c) and ("KB" in c or "MB" in c)), ""),
                        "updated": next((c for c in cells if re.search(r"\d{1,2}/\d{1,2}/\d{4}", c)), ""),
                    }
                )
                found += 1
            if found == 0:
                break
            page += 1
        # de-dup by name
        seen, uniq = set(), []
        for f in out:
            if f["name"] not in seen:
                seen.add(f["name"])
                uniq.append(f)
        return uniq

    def fetch(self, url: str) -> bytes:
        r = self.s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content


# --------------------------------------------------------------------------- publishedprices (Cerberus)
class PublishedPrices:
    """url.publishedprices.co.il — Rami Levy (RamiLevi), Tiv Taam (TivTaam) and others.
    Login with username, empty password."""

    BASE = "https://url.publishedprices.co.il"

    def __init__(self, chain: str, username: str, password: str = ""):
        self.chain = chain
        self.username = username
        self.password = password
        self.s = _session()
        self._logged = False

    def _csrf(self, html: str) -> str | None:
        m = re.search(r'name="csrftoken"\s+content="([^"]+)"', html) or re.search(r'csrftoken["\']?\s*[:=]\s*["\']([^"\']+)', html)
        return m.group(1) if m else None

    def login(self):
        r = self.s.get(f"{self.BASE}/login", timeout=TIMEOUT)
        r.raise_for_status()
        tok = self._csrf(r.text)
        data = {"username": self.username, "password": self.password, "r": "", "csrftoken": tok or ""}
        r = self.s.post(f"{self.BASE}/login/user", data=data, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        r = self.s.get(f"{self.BASE}/file", timeout=TIMEOUT)
        r.raise_for_status()
        self.token = self._csrf(r.text)
        if not self.token:
            raise RuntimeError(f"{self.chain}: login failed (no csrf token after login)")
        self._logged = True

    def list_files(self, kind: str, store_id=None) -> list[dict]:
        if not self._logged:
            self.login()
        data = {
            "sEcho": 1,
            "iColumns": 5,
            "sColumns": ",,,,",
            "iDisplayStart": 0,
            "iDisplayLength": 100000,
            "mDataProp_0": "fname",
            "mDataProp_1": "typeLabel",
            "mDataProp_2": "size",
            "mDataProp_3": "ftime",
            "mDataProp_4": "",
            "sSearch": kind,
            "bRegex": "false",
            "iSortingCols": 0,
            "cd": "/",
            "csrftoken": self.token,
        }
        r = self.s.post(f"{self.BASE}/file/json/dir", data=data, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        out = []
        for row in js.get("aaData", []):
            fname = row.get("fname") or row.get("name")
            if not fname:
                continue
            mm = re.match(r"(Price|PriceFull|Promo|PromoFull|Stores)(\d+)-(\d+)-(\d{8,12})", fname)
            if not mm or mm.group(1) != kind:
                continue
            st = int(mm.group(3))
            if store_id is not None and st != int(store_id):
                continue
            out.append(
                {
                    "name": fname,
                    "url": f"{self.BASE}/file/d/{fname}",
                    "store_id": st,
                    "store_name": "",
                    "size": row.get("size", ""),
                    "updated": row.get("ftime", ""),
                }
            )
        return out

    def fetch(self, url: str) -> bytes:
        if not self._logged:
            self.login()
        r = self.s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content


# --------------------------------------------------------------------------- Carrefour (u-code platform)
class Carrefour:
    """prices.carrefour.co.il — u-code.net listing. Discovery-first: the adapter tries
    the known u-code JSON/HTML endpoints and falls back to scraping anchor tags."""

    chain = "carrefour"
    BASE = "https://prices.carrefour.co.il"

    def __init__(self):
        self.s = _session()

    def _links_from_html(self, html: str) -> list[dict]:
        out = []
        for m in re.finditer(r'href="([^"]+\.(?:gz|xml|zip)(?:\?[^"]*)?)"', html, flags=re.I):
            href = m.group(1).replace("&amp;", "&")
            if href.startswith("/"):
                href = self.BASE + href
            fname = href.split("/")[-1].split("?")[0]
            mm = re.match(r"(Price|PriceFull|Promo|PromoFull|Stores)(\d+)-(\d+)-(\d{8,12})", fname)
            out.append({"name": fname, "url": href, "kind": mm.group(1) if mm else None, "store_id": int(mm.group(3)) if mm else None, "store_name": "", "size": "", "updated": ""})
        return out

    def discover(self) -> dict:
        """Return raw material for adapter design: index html + any JSON endpoints that answer."""
        info = {}
        r = self.s.get(self.BASE + "/", timeout=TIMEOUT)
        info["index_status"] = r.status_code
        info["index_html"] = r.text
        for ep in ["/api/files", "/files", "/file", "/list", "/api/list", "/getfiles", "/api/getfiles", "/?page=1", "/Prices", "/prices"]:
            try:
                rr = self.s.get(self.BASE + ep, timeout=TIMEOUT)
                info[ep] = {"status": rr.status_code, "ctype": rr.headers.get("content-type", ""), "head": rr.text[:3000]}
            except Exception as e:  # noqa
                info[ep] = {"error": str(e)}
        return info

    def list_files(self, kind: str, store_id=None, max_pages: int = 80) -> list[dict]:
        out = []
        # u-code sites commonly accept ?page=N and a text filter; try both plain and filtered
        for page in range(1, max_pages + 1):
            got = 0
            for url in (f"{self.BASE}/?page={page}", f"{self.BASE}/?type={kind}&page={page}", f"{self.BASE}/?q={kind}&page={page}"):
                try:
                    r = self.s.get(url, timeout=TIMEOUT)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                links = [l for l in self._links_from_html(r.text) if l["kind"] == kind]
                if links:
                    out.extend(links)
                    got += len(links)
                    break
            if got == 0:
                break
        seen, uniq = set(), []
        for f in out:
            if f["name"] not in seen and (store_id is None or f["store_id"] == int(store_id)):
                seen.add(f["name"])
                uniq.append(f)
        return uniq

    def fetch(self, url: str) -> bytes:
        r = self.s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content


def make_adapters(cfg: dict) -> dict:
    ads = {}
    for ch in cfg["chains"]:
        name = ch["chain"]
        if name in ("shufersal", "shufersal_online"):
            ads[name] = Shufersal()
            ads[name].chain = name
        elif name == "carrefour":
            ads[name] = Carrefour()
        elif ch.get("platform") == "publishedprices":
            ads[name] = PublishedPrices(name, ch["username"], ch.get("password", ""))
        else:
            raise ValueError(f"unknown chain config: {ch}")
    return ads
