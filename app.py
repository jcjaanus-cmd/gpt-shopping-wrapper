import os, math, datetime
from flask import Flask, request, jsonify
from paapi5_python_sdk import DefaultApi, ApiClient, Configuration
from paapi5_python_sdk.models import SearchItemsRequest

# ========= ENV VARS (set in Render, not in code) =========
AMAZON_ACCESS_KEY   = os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY   = os.getenv("AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG  = os.getenv("AMAZON_PARTNER_TAG")       # e.g., yourtag-20
AMAZON_HOST         = os.getenv("AMAZON_HOST", "webservices.amazon.com")
AMAZON_REGION       = os.getenv("AMAZON_REGION", "us-east-1")
AMAZON_MARKETPLACE  = os.getenv("AMAZON_MARKETPLACE", "www.amazon.com")

# ========= FLASK =========
app = Flask(__name__)

# ========= AMAZON CLIENT =========
def get_paapi_client():
    cfg = Configuration(
        access_key=AMAZON_ACCESS_KEY,
        secret_key=AMAZON_SECRET_KEY,
        host=AMAZON_HOST,
        region=AMAZON_REGION,
    )
    return DefaultApi(ApiClient(cfg))

# ========= NORMALIZE ONE ITEM =========
def normalize_item(item):
    try:
        asin = getattr(item, "asin", None)

        title = None
        if item.item_info and item.item_info.title:
            title = item.item_info.title.display_value

        brand = None
        category = None
        upc = ean = None
        if item.item_info:
            if item.item_info.by_line_info and item.item_info.by_line_info.brand:
                brand = item.item_info.by_line_info.brand.display_value
            if item.item_info.classifications and item.item_info.classifications.binding:
                category = item.item_info.classifications.binding.display_value
            if item.item_info.external_ids:
                if item.item_info.external_ids.upcs and item.item_info.external_ids.upcs.display_values:
                    upc = item.item_info.external_ids.upcs.display_values[0]
                if item.item_info.external_ids.eans and item.item_info.external_ids.eans.display_values:
                    ean = item.item_info.external_ids.eans.display_values[0]

        price_amt = list_price_amt = savings_amt = savings_pct = None
        stock_msg = None
        is_prime = is_free_shipping = is_fba = None
        delivery_min = delivery_max = None

        if item.offers and item.offers.listings:
            listing = item.offers.listings[0]

            if listing.price and listing.price.amount is not None:
                price_amt = float(listing.price.amount)

            if listing.saving_basis and listing.saving_basis.amount is not None:
                list_price_amt = float(listing.saving_basis.amount)
                if price_amt is not None:
                    savings_amt = max(0.0, list_price_amt - price_amt)
                    if list_price_amt > 0:
                        savings_pct = round(100.0 * savings_amt / list_price_amt)

            if listing.availability and listing.availability.message:
                stock_msg = listing.availability.message

            if listing.merchant_info:
                is_fba = bool(listing.merchant_info.is_fulfilled_by_amazon)

            if listing.delivery_info:
                is_prime = bool(listing.delivery_info.is_prime_eligible) if listing.delivery_info.is_prime_eligible is not None else None
                is_free_shipping = bool(listing.delivery_info.is_free_shipping_eligible) if listing.delivery_info.is_free_shipping_eligible is not None else None
                if listing.delivery_info.min_delivery_date:
                    delivery_min = str(listing.delivery_info.min_delivery_date)[:10]
                if listing.delivery_info.max_delivery_date:
                    delivery_max = str(listing.delivery_info.max_delivery_date)[:10]

        rating = reviews = None
        if item.customer_reviews:
            if item.customer_reviews.star_rating and item.customer_reviews.star_rating.display_value is not None:
                rating = float(item.customer_reviews.star_rating.display_value)
            if item.customer_reviews.count is not None:
                reviews = int(item.customer_reviews.count)

        image_url = None
        image_variants = []
        if item.images:
            if item.images.primary and item.images.primary.large and item.images.primary.large.url:
                image_url = item.images.primary.large.url
            if item.images.variants:
                for v in item.images.variants:
                    if v.large and v.large.url:
                        image_variants.append(v.large.url)

        features = []
        if item.item_info and item.item_info.features and item.item_info.features.display_values:
            features = item.item_info.features.display_values[:6]

        link = getattr(item, "detail_page_url", None)

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
            "is_free_shipping": is_free_shipping,
            "is_fulfilled_by_amazon": is_fba,
            "delivery_min": delivery_min,
            "delivery_max": delivery_max,
            "features": features,
            "image": image_url,
            "images": image_variants[:5],
            "external_ids": {"upc": upc, "ean": ean},
            "affiliate_url": link,
        }
    except Exception:
        return None

# ========= HEALTH =========
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": datetime.datetime.utcnow().isoformat() + "Z"})

# ========= SEARCH =========
@app.get("/search")
def search():
    if not (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_PARTNER_TAG):
        return jsonify({"error":"Missing Amazon credentials env vars"}), 500

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error":"Missing required query param 'q'"}), 400

    def _float(name):
        v = request.args.get(name)
        try:
            return float(v) if v else None
        except:
            return None

    def _int(name, default):
        v = request.args.get(name)
        try:
            return int(v) if v else default
        except:
            return default

    def _bool(name):
        v = (request.args.get(name) or "").lower().strip()
        return True if v in ("1", "true", "yes", "y") else False

    max_price  = _float("max_price")
    pages      = max(1, min(_int("pages", 2), 10))
    prime_only = _bool("prime_only")

    resources = [
        "Images.Primary.Large",
        "Images.Variants.Large",
        "ItemInfo.Title",
        "ItemInfo.Features",
        "ItemInfo.ByLineInfo",
        "ItemInfo.ProductInfo",
        "ItemInfo.Classifications",
        "ItemInfo.ExternalIds",
        "Offers.Listings.Price",
        "Offers.Listings.SavingBasis",
        "Offers.Listings.Savings",
        "Offers.Listings.MerchantInfo",
        "Offers.Listings.DeliveryInfo.IsPrimeEligible",
        "Offers.Listings.DeliveryInfo.IsFreeShippingEligible",
        "Offers.Listings.DeliveryInfo.MinDeliveryDate",
        "Offers.Listings.DeliveryInfo.MaxDeliveryDate",
        "Offers.Listings.Availability.Message",
        "CustomerReviews.Count",
        "CustomerReviews.StarRating",
    ]

    api = get_paapi_client()
    results = []

    for page in range(1, pages + 1):
        req = SearchItemsRequest(
            partner_tag=AMAZON_PARTNER_TAG,
            partner_type="Associates",
            marketplace=AMAZON_MARKETPLACE,
            keywords=q,
            item_count=10,
            item_page=page,
            resources=resources
        )
        try:
            resp = api.search_items(req)
        except Exception:
            break

        if not resp or not resp.items_result or not resp.items_result.items:
            break

        for raw in resp.items_result.items:
            norm = normalize_item(raw)
            if not norm:
                continue
            if max_price is not None and norm["price"] is not None and norm["price"] > max_price:
                continue
            if prime_only and norm["is_prime"] is not True:
                continue
            results.append(norm)

    ts = datetime.datetime.utcnow().isoformat() + "Z"

    def score(p):
        r = p["rating"] or 0.0
        v = math.log10((p["review_count"] or 0) + 1.0)
        price_fit = 0.0
        if max_price and p["price"]:
            price_fit = 1.0 - min(1.0, max(0.0, (p["price"] / max_price)))
        prime_bonus = 0.2 if p["is_prime"] else 0.0
        return (r * 2.0) + v + price_fit + prime_bonus

    results_sorted = sorted(results, key=score, reverse=True)

    return jsonify({
        "criteria": {"q": q, "max_price": max_price, "prime_only": prime_only, "pages": pages},
        "timestamp": ts,
        "products": results_sorted
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
