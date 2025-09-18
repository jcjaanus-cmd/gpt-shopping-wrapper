import os, datetime, time, threading, random, math, json
from flask import Flask, request, jsonify
import requests
from requests_aws4auth import AWS4Auth

# ========= ENV =========
AMAZON_ACCESS_KEY   = os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY   = os.getenv("AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG  = os.getenv("AMAZON_PARTNER_TAG")
AMAZON_HOST         = os.getenv("AMAZON_HOST", "webservices.amazon.com")
AMAZON_REGION       = os.getenv("AMAZON_REGION", "us-east-1")
AMAZON_MARKETPLACE  = os.getenv("AMAZON_MARKETPLACE", "www.amazon.com")
AMAZON_DOMAIN       = os.getenv("AMAZON_DOMAIN", "amazon.com")

SERVICE             = "ProductAdvertisingAPI"
ENDPOINT_SEARCH     = f"https://{AMAZON_HOST}/paapi5/searchitems"

CACHE_TTL_SECONDS   = int(os.getenv("CACHE_TTL_SECONDS", "180"))
MIN_CALL_INTERVAL   = float(os.getenv("MIN_CALL_INTERVAL", "1.1"))

app = Flask(__name__)

# ========= helpers =========
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

        stock_msg = g(listing, ["Availability","Message"])
        is_prime  = g(listing, ["DeliveryInfo","IsPrimeEligible"])
        is_free   = g(listing, ["DeliveryInfo","IsFreeShippingEligible"])

        rating  = g(item, ["CustomerReviews","StarRating","DisplayValue"])
        reviews = g(item, ["CustomerReviews","Count"])

        image   = g(item, ["Images","Primary","Large","URL"])
        variants = []
        for v in (g(item, ["Images","Variants"], []) or []):
            url = g(v, ["Large","URL"])
            if url: variants.append(url)

        features = g(item, ["ItemInfo","Features","DisplayValues"], []) or []

        link = item.get("DetailPageURL")

        return {
            "id": asin,
            "asin": asin,
            "name": title,
            "brand": brand,
            "category": category,
            "price": price_amt,
            "currency": "USD",
            "rating": rating,
            "review_count": reviews,
            "stock_message": stock_msg,
            "is_prime": is_prime,
            "is_free_shipping": is_free,
            "features": features[:6],
            "image": image,
            "images": variants[:5],
            "affiliate_url": link
        }
    except Exception:
        return None

# ========= cache & rate limiting =========
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        exp, data = item
        if time.time() > exp:
            try: del _cache[key]
            except: pass
            return None
        return data

def cache_set(key, data, ttl=CACHE_TTL_SECONDS):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, data)

_rate_lock = threading.Lock()
_last_call_ts = 0.0

def _rate_limit():
    global _last_call_ts
    with _rate_lock:
        now = time.time()
        wait = MIN_CALL_INTERVAL - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()

def _paapi_headers():
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
        "Content-Encoding": "amz-1.0",
        "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
    }

# ========= Amazon PA-API =========
def search_paapi(keywords, item_page, item_count, resources):
    if not (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG):
        raise RuntimeError("Missing Amazon credentials env vars")

    auth = AWS4Auth(AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_REGION, SERVICE)
    body = {
        "PartnerTag": AMAZON_PARTNER_TAG,
        "PartnerType": "Associates",
        "Marketplace": AMAZON_MARKETPLACE,
        "Keywords": keywords,
        "ItemPage": item_page,
        "ItemCount": item_count,
        "Resources": resources,
    }

    _rate_limit()
    r = requests.post(ENDPOINT_SEARCH, json=body, headers=_paapi_headers(),
                      auth=auth, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = g(data, ["SearchResult","Items"]) or g(data, ["ItemsResult","Items"], [])
    return items or []

# ========= Rainforest =========
def _normalize_rainforest_item(r):
    price = g(r, ["price", "value"])
    currency = g(r, ["price", "currency"]) or "USD"
    return {
        "id": r.get("asin"),
        "asin": r.get("asin"),
        "name": r.get("title"),
        "brand": r.get("brand"),
        "category": None,
        "price": float(price) if price is not None else None,
        "currency": currency,
        "rating": g(r, ["rating"]),
        "review_count": g(r, ["ratings_total"]),
        "image": r.get("image"),
        "affiliate_url": r.get("link"),
    }

def search_rainforest(q, page=1, num=10):
    api_key = os.getenv("RAINFOREST_API_KEY")
    if not api_key:
        raise RuntimeError("Missing RAINFOREST_API_KEY")
    r = requests.get(
        "https://api.rainforestapi.com/request",
        params={
            "api_key": api_key,
            "type": "search",
            "amazon_domain": AMAZON_DOMAIN,
            "search_term": q,
            "page": page
        },
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("search_results") or []
    out = []
    for it in items[:num]:
        norm = _normalize_rainforest_item(it)
        if norm: out.append(norm)
    return out

# ========= routes =========
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "has_amazon": bool(AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG),
        "has_rainforest": bool(os.getenv("RAINFOREST_API_KEY")),
    })

@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error":"Missing required query param 'q'"}), 400

    provider = (request.args.get("provider") or "rainforest").lower()

    def _float(name): 
        v = request.args.get(name); 
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
    pages      = max(1, min(_int("pages", 1), 5))   # default 1 page
    prime_only = _bool("prime_only")

    resources = [
        "Images.Primary.Large","Images.Variants.Large",
        "ItemInfo.Title","ItemInfo.Features","ItemInfo.ByLineInfo",
        "ItemInfo.ProductInfo","ItemInfo.Classifications","ItemInfo.ExternalIds",
        "Offers.Listings.Price","Offers.Listings.SavingBasis","Offers.Listings.Savings",
        "Offers.Listings.MerchantInfo",
        "Offers.Listings.DeliveryInfo.IsPrimeEligible",
        "Offers.Listings.DeliveryInfo.IsFreeShippingEligible",
        "Offers.Listings.Availability.Message",
        "CustomerReviews.Count","CustomerReviews.StarRating"
    ]

    results = []
    errors = []

    try:
        if provider == "rainforest":
            # light: only page 1, ~10 items
            results = search_rainforest(q, page=1, num=10)
        else:
            if not (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG):
                return jsonify({"error":"Missing Amazon credentials env vars"}), 500

            for page in range(1, pages+1):
                cache_key = ("search", q, page)
                cached = cache_get(cache_key)
                if cached is None:
                    raw = search_paapi(q, page, 10, resources)
                    normalized = [normalize_item(it) for it in raw if normalize_item(it)]
                    cache_set(cache_key, normalized)
                else:
                    normalized = cached

                # simple filters (Amazon path only)
                for p in normalized:
                    if (max_price is not None) and (p["price"] is not None) and (p["price"] > max_price):
                        continue
                    if prime_only and p["is_prime"] is not True:
                        continue
                    results.append(p)

                time.sleep(0.2)
    except Exception as e:
        errors.append(str(e))

    ts = datetime.datetime.utcnow().isoformat() + "Z"

    # light scoring (kept for backwards compatibility)
    def score(p):
        r = (p.get("rating") or 0.0)
        v = math.log10((p.get("review_count") or 0) + 1.0)
        price_fit = 0.0
        if max_price and p.get("price"):
            price_fit = 1.0 - min(1.0, max(0.0, (p["price"]/max_price)))
        prime_bonus = 0.2 if p.get("is_prime") else 0.0
        return (r*2.0) + v + price_fit + prime_bonus

    results_sorted = sorted(results, key=score, reverse=True)

    payload = {
        "criteria": {"q": q, "provider": provider, "max_price": max_price, "prime_only": prime_only, "pages": pages},
        "timestamp": ts,
        "products": results_sorted[:50]  # cap at 50
    }
    if errors:
        payload["errors"] = errors

    status = 200 if results_sorted else (502 if errors else 200)
    return jsonify(payload), status

# local run
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
