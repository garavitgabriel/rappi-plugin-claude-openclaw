"""Base URL, endpoints, and header templates for the Rappi API."""

import os

IMAGES_BASE_URL = "https://images.rappi.com"

# Supported countries
# - domain: login page, origin/referer headers
# - api_prefix: base URL is services.{prefix}grability.rappi.com (CO has no prefix)
COUNTRIES = {
    "co": {"domain": "www.rappi.com.co", "api_prefix": "", "name": "Colombia"},
    "mx": {"domain": "www.rappi.com.mx", "api_prefix": "mx", "name": "Mexico"},
}

# Country is set via RAPPI_COUNTRY env var or config — defaults to Colombia
_country_code = os.environ.get("RAPPI_COUNTRY", "co").lower()
_country = COUNTRIES.get(_country_code, COUNTRIES["co"])

RAPPI_DOMAIN = _country["domain"]
_api_prefix = _country["api_prefix"]
BASE_URL = f"https://services.{_api_prefix}grability.rappi.com" if _api_prefix else "https://services.grability.rappi.com"
# The order-history endpoint lives on the v2 gateway host, not the services host.
V2_BASE_URL = f"https://v2.{_api_prefix}grability.rappi.com" if _api_prefix else "https://v2.grability.rappi.com"

# Default coordinates (0,0 — auto-synced from active address at runtime)
DEFAULT_LAT = 0.0
DEFAULT_LNG = 0.0

# Mobile user agent matching the Rappi web app
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36"
)

APP_VERSION = "e1de6be43aa29091011474615d7ac0810051c36a"

# Different parts of the Rappi web app use different x-application-id values
WEB_VERSION = "web_v1.154.3"
CHECKOUT_VERSION = "v1.80.0"


def build_headers(token: str, device_id: str) -> dict[str, str]:
    """Build the full header set required by every Rappi API request."""
    headers = {
        "authorization": f"Bearer {token}",
        "deviceid": device_id,
        "accept": "application/json",
        "accept-language": "es-CO",
        "app-version": APP_VERSION,
        "needappsflyerid": "false",
        "origin": f"https://{RAPPI_DOMAIN}",
        "referer": f"https://{RAPPI_DOMAIN}/",
        "user-agent": USER_AGENT,
        "vendor": "rappi",
        "x-application-id": f"rappi-microfront-web/{APP_VERSION}",
        "sec-ch-ua": '"Not-A.Brand";v="24", "Chromium";v="146"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    }
    return headers


# Context-specific header overrides for endpoints that need different app IDs
HEADERS_BROWSE = {
    "x-application-id": WEB_VERSION,
    "app-version": WEB_VERSION,
    "include_context_info": "true",
    "language": "es",
}

HEADERS_CHECKOUT = {
    "x-application-id": f"rappi-checkout-web/{CHECKOUT_VERSION}",
    "app-version": CHECKOUT_VERSION,
}

HEADERS_FAVORITES = {
    "content-type": "application/json; charset=UTF-8",
}


# --- Endpoint paths ---

class Endpoints:
    # Auth / User
    USER_PROFILE = "/ms/application-user/auth"
    IS_PRIME = "/api/ms/rappi-prime/is-prime"

    # Address
    REVERSE_GEOCODE = "/api/ms/address/reverse-geocode"
    LIST_ADDRESSES = "/api/ms/users-address/addresses"
    SET_ACTIVE_ADDRESS = "/api/ms/users-address/addresses/{address_id}/active"

    # Search
    UNIFIED_SEARCH = "/api/pns-global-search-api/v1/unified-search"

    # Stores
    RESTAURANT_CATALOG = "/api/restaurant-bus/stores/catalog-paged/home"
    STORE_DETAIL = "/api/web-gateway/web/stores-router/id/{store_id}/"  # trailing slash required
    STORE_MENU = "/api/restaurant-bus/store/{store_id}/menu"  # separate endpoint for menu corridors
    PRODUCT_TOPPINGS = "/api/web-gateway/web/restaurants-bus/products/toppings/{store_id}/{product_id}/"  # trailing slash required

    # Cart
    CART_ADD = "/api/ms/shopping-cart/v2/{store_type}/store"
    CART_GET_ALL = "/api/ms/shopping-cart/v1/all/get"
    CART_REMOVE = "/api/ms/shopping-cart/v2/{store_type}/product/{compound_product_id}"
    CART_RECALCULATE = "/api/ms/shopping-cart/v1/{store_type}/recalculate"

    # Checkout
    CHECKOUT_DETAIL = "/api/ms/shopping-cart/v1/{store_type}/checkout/detail"
    CHECKOUT_WIDGETS = "/api/ms/checkout-component/{store_type}"
    SET_TIP = "/api/ms/shopping-cart/v1/{store_type}/tip"
    TIP_SUGGESTIONS = "/api/ms/core-tip/user-segmentation"
    SET_PAYMENT_METHOD = "/api/ms/shopping-cart/v1/{store_type}/payment-method"
    PLACE_ORDER = "/api/ms/shopping-cart-proxy/{store_type}/checkout"

    # Orders
    GET_ORDERS = "/api/user-order-home/orders"
    ACTIVE_ORDERS_V3 = "/api/user-order-home/v3/orders"
    ORDER_HISTORY = "/api/orders/history-user"  # completed-order history (paginated); on V2_BASE_URL
    ORDER_RESUME = "/order-resume/fully/{order_id}"
    ORDER_REALTIME_STATE = "/api/ms/user-order-state/auth/{order_id}"
    ORDER_PRODUCTS = "/api/support-order-cost/orders/{order_id}/products"
    ORDER_COST_BREAKDOWN = "/api/support-order-cost/orders/{order_id}/costs-and-discounts"

    # Homepage / Verticals
    HOME_VERTICALS = "/home/high/"
    STORE_TYPE_HIERARCHY = "/api/web-gateway/web/stores-router/available/principal/"

    # Dynamic Content (store browsing — aisles, categories, store info)
    DYNAMIC_CONTENT = "/api/web-gateway/web/dynamic/context/content/"

    # CPG In-Store Search (Turbo, markets — richer than unified search)
    CPG_PRODUCT_SEARCH = "/api/cpgs/search/v2/store/{store_id}/products"

    # Payment
    DEFAULT_PAYMENT_METHOD = "/api/ms/payment-method/default-payment-method"

    # Account
    FAVORITE_STORES_API = "/api/pns-global-search-api/v1/unified-favorite-stores"
    RAPPI_CREDITS = "/api/ms/rappi-credits-mongo/"


def resolve_image_url(path: str | None, prefix: str = "products") -> str | None:
    """Resolve a relative image path to a full URL."""
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{IMAGES_BASE_URL}/{prefix}/{path}"
