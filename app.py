import os, math, datetime, json
from flask import Flask, request, jsonify
import requests
from requests_aws4auth import AWS4Auth

# ========== ENV VARS ==========
AMAZON_ACCESS_KEY   = os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY   = os.getenv("AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG  = os.getenv("AMAZON_PARTNER_TAG")      # e.g., yourtag-20
AMAZON_HOST         = os.getenv("AMAZON_HOST", "webservices.amazon.com")
AMAZON_REGION       = os.getenv("AMAZON_REGION", "us-east-1")
AMAZON_MARKETPLACE  = os.getenv("AMAZON_MARKETPLACE", "www.amazon.com")

SERVICE             = "ProductAdvertisingAPI"
ENDPOINT_SEARCH     = f"https://{AMAZON_HOST}/paapi5/searchitems"

app = Flask(__name__)

# ---------- helpers ----------
def g(obj, path, default=None):
    cur = obj
    try:
        for k in path:
            if isinstance(cur, dict):
                cur = cur.get(k, None)
            else:
                return default
            if cur is None:
                return default
        return cur
    except Exception:
        return default

def normalize_item(item):
    try:
        asin   = item.get("ASIN")
        title  = g(item, ["ItemInfo","Title","DisplayValue"])
        brand  = g(item, ["ItemInfo","ByLineInfo","Brand","DisplayValue"])
        category = g(item, ["ItemInfo","Classifications","Binding","DisplayValue"])

        listing = g(item, ["Offers","Listings",0], {})
        price_amt = g(listing, ["Price","Amount"])
        list_price_amt = g(listing, ["SavingBasis","Amount"])
        savings_amt = savings_pct = None
        if price_amt is not None and list_price_amt:
            savings_amt = max(0.0, list_price_amt - price_amt)
            if list_price_amt > 0:
                savings_pct = round(100.0 * savings_amt / list_price_amt)

        stock_msg = g(listing, ["Availability","Message"])
        is_prime  = g(listing, ["DeliveryInfo","IsPrimeEligible"])
        is_free   = g(listing, ["DeliveryInfo","IsFreeShippingEligible"])
        del_min   = g(listing, ["DeliveryInfo","MinDeliveryDate"])
        del_max   = g(listing, ["DeliveryInfo","MaxDeliveryDate"])
        if del_min: del_min = str(del_min)[:10]
        if del_max: del_max = str(del_max)[:10]

        rating  = g(item, ["CustomerReviews","StarRating","DisplayValue"])
        reviews = g(item, ["CustomerReviews","Count"])

        image   = g(item, ["Images","Primary","Large","URL"])
        variants = []
        for v in (g(item, ["Images","Variants"], []) or []):
            url = g(v, ["Large","URL"])
            if url: variants.append(url)

        features = g(item, ["ItemInfo","Features","DisplayValues"], []) or []

        upc = None
        ean = None
        upcs = g(item, ["ItemInfo","ExternalIds","UPCs","DisplayValues"])
        eans = g(item, ["ItemInfo","ExternalIds","EANs","DisplayValues"])
        if upcs: upc = upcs[0]
        if eans: ean = eans[0]

        link = item.get("DetailPageURL")

        return {
            "id": asin,
            "asin": asin,
            "name": title,
            "brand": brand,
            "category": category,
            "price": price_amt,
            "currency": "USD",
            "list_price": list_price_amt,
            "savings_amount": savings_amt,
            "savings_percent": savings_pct,
            "rating": rating,
            "review_count": reviews,
            "stock_message": stock_msg,
            "is_prime": is_prime,
            "is_free_shipping": is_free,
            "is_fulfilled_by_amazon": g(listing, ["MerchantInfo","IsFulfilledByAmazon"]),
            "delivery_min": del_min,
            "delivery_max": del_max,
            "features": features[:6],
            "image": image,
            "images": variants[:5],
            "external_ids": {"upc": upc, "ean": ean},
            "affiliate_url": link
        }
    except Exception:
        return None

def search_paapi(keywords, item_page, item_count, resources):
    auth = AWS4Auth(AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_REGION, SERVICE)
    headers = {"Content-Type":"application/json; charset=UTF-8", "Accept":"application/json"}
    body = {
        "PartnerTag": AMAZON_PARTNER_TAG,
        "PartnerType": "Associates",
        "Marketplace": AMAZON_MARKETPLACE,
        "Keywords": keywords,
        "ItemPage": item_page,
        "ItemCount": item_count,
        "Resources": resources
    }
    r = requests.post(ENDPOINT_SEARCH, json=body, headers=headers, auth=auth, timeout=15)
    # If Amazon returns a JSON error payload with 400/401/403, surface it
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        err_text = r.text
        try:
            err_json = r.json()
        except Exception:
            err_json = None
        raise RuntimeError(f"PA-API HTTP {r.status_code}: {err_text if not err_json else json.dumps(err_json)}") from e

    data = r.json()
    # Surface per-call logical errors (Errors array) if they exist
    if "Errors" in data and data["Errors"]:
        raise RuntimeError(f"PA-API Errors: {json.dumps(data['Errors'])}")

    items = g(data, ["SearchResult","Items"]) or g(data, ["ItemsResult","Items"], [])
    return items or []

# ---------- routes ----------
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "env_ok": bool(AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG),
        "host": AMAZON_HOST,
        "region": AMAZON_REGION,
        "marketplace": AMAZON_MARKETPLACE
    })

@app.get("/search")
def search():
    if not (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG):
        return jsonify({"error":"Missing Amazon credentials env vars"}), 500

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error":"Missing required query param 'q'"}), 400

    def _float(name):
        v = request.args.get(name)
        try: return float(v) if v else None
        except: return None
    def _int(name, default):
        v = request.args.get(name)
        try: return int(v) if v else default
        except: return default
    def _bool(name):
        v = (request.args.get(name) or "").lower().strip()
        return True if v in ("1","true","yes","y") else False

    max_price  = _float("max_price")
    pages      = max(1, min(_int("pages", 2), 10))
    prime_only = _bool("prime_only")

    resources = [
        "Images.Primary.Large","Images.Variants.Large",
        "ItemInfo.Title","ItemInfo.Features","ItemInfo.ByLineInfo",
        "ItemInfo.ProductInfo","ItemInfo.Classifications","ItemInfo.ExternalIds",
        "Offers.Listings.Price","Offers.Listings.SavingBasis","Offers.Listings.Savings",
        "Offers.Listings.MerchantInfo",
        "Offers.Listings.DeliveryInfo.IsPrimeEligible",
        "Offers.Listings.DeliveryInfo.IsFreeShippingEligible",
        "Offers.Listings.DeliveryInfo.MinDeliveryDate",
        "Offers.Listings.DeliveryInfo.MaxDeliveryDate",
        "Offers.Listings.Availability.Message",
        "CustomerReviews.Count","CustomerReviews.StarRating"
    ]

    results = []
    errors = []
    for page in range(1, pages+1):
        try:
            items = search_paapi(q, page, 10, resources)
        except Exception as e:
            errors.append(str(e))
            break
        if not items:
            break
        for raw in items:
            norm = normalize_item(raw)
            if not norm:
                continue
            if (max_price is not None) and (norm["price"] is not None) and (norm["price"] > max_price):
                continue
            if prime_only and norm["is_prime"] is not True:
                continue
            results.append(norm)

    ts = datetime.datetime.utcnow().isoformat() + "Z"

    def score(p):
        r = (p["rating"] or 0.0)
        v = math.log10((p["review_count"] or 0) + 1.0)
        price_fit = 0.0
        if max_price and p["price"]:
            price_fit = 1.0 - min(1.0, max(0.0, (p["price"]/max_price)))
        prime_bonus = 0.2 if p["is_prime"] else 0.0
        return (r*2.0) + v + price_fit + prime_bonus

    results_sorted = sorted(results, key=score, reverse=True)

    payload = {
        "criteria": {"q": q, "max_price": max_price, "prime_only": prime_only, "pages": pages},
        "timestamp": ts,
        "products": results_sorted
    }
    if errors:
        payload["errors"] = errors
    status = 200 if results_sorted else (502 if errors else 200)
    return jsonify(payload), status

@app.get("/debug-search")
def debug_search():
    q = request.args.get("q", "headphones")
    try:
        items = search_paapi(q, 1, 10, [
            "ItemInfo.Title", "Offers.Listings.Price"
        ])
        return jsonify({"ok": True, "q": q, "count": len(items), "items_raw_keys": list(items[0].keys()) if items else []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
