"""Rappi MCP server — exposes Rappi services as tools for AI assistants."""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress

from rappi.auth_refresh import RefreshFailedError, needs_proactive_refresh, refresh_tokens
from rappi.client import RappiClient
from rappi.config import ConfigManager
from rappi.memory import MemoryManager
from rappi.models.store import Product, StoreDetail, ToppingCategory
from rappi.services.address import list_addresses as _list_addresses
from rappi.services.address import set_active_address as _set_active_address
from rappi.services.auth import get_profile as _get_profile
from rappi.services.auth import is_prime as _is_prime
from rappi.services.cart import add_to_cart as _add_to_cart
from rappi.services.cart import get_carts as _get_carts
from rappi.services.cart import recalculate_cart as _recalculate_cart
from rappi.services.cart import remove_from_cart as _remove_from_cart
from rappi.services.checkout import get_checkout_detail as _get_checkout_detail
from rappi.services.checkout import place_order as _place_order
from rappi.services.checkout import set_tip as _set_tip
from rappi.services.order import get_orders as _get_orders
from rappi.services.order import get_order_resume as _get_order_resume
from rappi.services.order import get_order_realtime_state as _get_order_realtime_state
from rappi.services.order import get_order_cost_breakdown as _get_order_cost_breakdown
from rappi.services.order import fetch_order_history as _fetch_order_history
from rappi.services.search import search as _search
from rappi.services.search import search_cpg_products as _search_cpg_products
from rappi.services.home import get_home_verticals as _get_home_verticals
from rappi.services.dynamic import get_store_aisles as _get_store_aisles
from rappi.services.dynamic import get_aisle_products as _get_aisle_products
from rappi.services.dynamic import get_store_information as _get_store_info
from rappi.services.account import get_favorite_stores_api as _get_favorite_stores_api
from rappi.services.account import get_rappi_credits as _get_rappi_credits
from rappi.services.account import get_active_orders_v3 as _get_active_orders_v3
from rappi.services.checkout import get_payment_methods as _get_payment_methods
from rappi.services.store import get_product_toppings as _get_toppings
from rappi.services.store import get_restaurant_catalog as _get_catalog
from rappi.services.store import get_store_detail as _get_store_detail
from rappi.services.store import search_store_products as _search_store_products
from rappi.utils.ids import make_compound_id
from rappi.utils.pricing import strip_html

from mcp.server.fastmcp import FastMCP

# Disable DNS rebinding protection for remote transports (Railway, etc.)
_transport = os.environ.get("MCP_TRANSPORT", "stdio")
_mcp_kwargs: dict = dict(
    name="rappi",
    instructions=(
        "Rappi food delivery tools for searching restaurants, browsing menus, "
        "managing a cart, and placing orders in Colombia (prices in COP).\n\n"
        "WORKFLOW: Start with get_ordering_context to understand current state. "
        "Then: search_restaurants or browse_restaurants -> get_restaurant_menu -> "
        "get_product_toppings (if has_toppings=true) -> add_to_cart -> "
        "checkout(confirm=false) to preview -> checkout(confirm=true) to place.\n\n"
        "The user must authenticate first via `rappi auth login` in their terminal. "
        "Tokens auto-refresh; if a tool still reports the token expired, the user must run "
        "`uv run rappi auth login` then `uv run rappi auth push-railway` (README § Auth runbook)."
    ),
)

if _transport in ("sse", "streamable-http", "http"):
    try:
        from mcp.server.auth.settings import TransportSecuritySettings
    except ImportError:
        from mcp.server.transport_security import TransportSecuritySettings  # type: ignore[no-redef]
    _mcp_kwargs["transport_security"] = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

mcp = FastMCP(**_mcp_kwargs)


# --- Resources (workflow playbooks for any MCP client) ---


@mcp.resource("rappi://workflow/ordering")
async def ordering_workflow() -> str:
    """Complete ordering workflow — the step-by-step playbook for placing an order."""
    return """# Rappi Ordering Workflow

Follow these steps in order. Skip steps that aren't needed.

## Step 1: Context
Call `get_ordering_context` FIRST. This returns:
- User profile (name, Prime status)
- Active delivery address
- Current cart contents (if any)
- Active orders being tracked
- Memory summary (order count, last order, favorites)
- Taste summary (one-line profile)

If not authenticated, tell user to run `rappi auth login`.

## Step 2: Find Food
Choose based on what the user wants:
- **Specific food/restaurant**: `search_restaurants(query)`
- **Browse nearby**: `browse_restaurants(offset, limit)`
- **Suggestions**: `get_recommendations(context)` — uses taste profile + time of day
- **Reorder**: `get_order_history(limit)` then `quick_reorder(order_id)`
- **Favorites**: `get_favorites()` then browse that store

Search returns ALL store types (restaurants, Turbo, markets, pharmacies).
Check the `store_type` field to know which kind of store it is.

## Step 3: Browse Menu
- **Restaurants** (store_type="restaurant"): `get_restaurant_menu(store_id)`
- **Turbo/markets/pharmacies**: `search_in_store(store_id, query)` — no static menu
- **Score items**: `score_menu(store_id)` ranks items by taste match

If `get_restaurant_menu` returns empty categories with a "hint", use `search_in_store`.

## Step 4: Toppings (IMPORTANT)
If a product has `has_toppings=true`:
1. Call `get_product_toppings(store_id, product_id)` BEFORE adding to cart
2. Categories with `min_required > 0` are MANDATORY
3. Present options to user, get their choices
4. Include selected `topping_ids` in `add_to_cart`

Skipping this for required toppings will cause `add_to_cart` to return an error.

## Step 5: Add to Cart
Call `add_to_cart(store_id, product_id, quantity, topping_ids)`.
- Price and product details are auto-fetched (no need to pass them)
- If error says "missing required toppings", go back to Step 4
- Ask if user wants to add more items

## Step 6: Checkout
1. Set the tip first: `set_tip(tip_amount)` — persists on Rappi's server
2. Call `checkout(confirm=false)` — PREVIEW ONLY
3. Show the summary to the user (products, delivery fee, tip, total)
4. Check preferences for default tip: `get_preferences()`
5. Ask user to confirm
6. ONLY after explicit confirmation: `checkout(confirm=true)`
7. NEVER place an order without user saying yes

## Step 7: Track
After placing: `get_order_status()` shows delivery state and ETA.
Order states: created → in_store → on_the_way → delivered

## Prices
All prices are in COP (Colombian Pesos). Format with dots: $35.500 (not commas).

## Error Recovery
- Token expired (auto-refresh failed) → tell user to run `uv run rappi auth login`, then `uv run rappi auth push-railway` for the hosted server
- Store unavailable → suggest alternatives
- Missing toppings → show which categories need selection
- Product out of stock → suggest similar items from menu
"""


@mcp.resource("rappi://workflow/personalization")
async def personalization_workflow() -> str:
    """How to use the memory and intelligence features for personalized experiences."""
    return """# Rappi Personalization Guide

## Understanding the User
Call `get_taste_profile()` to get the full computed profile:
- Category preferences: what cuisines they like (with percentages)
- Store type preferences: restaurant vs turbo vs market
- Price range: average order, average item, min/max
- Time patterns: when they order (morning/lunch/evening/night), which days
- Topping preferences: what customizations they always pick
- Top products: most reordered items
- Top stores: most ordered from
- Spending: total, average, tip habits, orders per week
- Dietary restrictions and allergies

## Making Recommendations
Call `get_recommendations(context)` for scored suggestions:
- "usual" — their regular order from specific stores (highest confidence)
- "time_based" — stores they order from at this time of day
- "similar_product" — products similar to their taste (requires embeddings)
- "new_store" — stores they haven't tried matching their preferences

Always respect dietary_restrictions and allergies from the profile.

## Scoring Menus
After fetching a menu, call `score_menu(store_id)` to rank items by taste match.
Present highest-scored items first: "Based on your taste, I'd recommend..."

## Memory Tools
- `get_order_history(limit)` — what they've ordered before
- `get_favorites()` — explicitly saved stores
- `get_preferences()` — dietary, allergies, default tip
- `set_preference(key, value)` — save new preferences
- `smart_search(query)` — search across cached products by meaning

## Embeddings
If embeddings are enabled (check `get_ordering_context` → memory.embeddings_enabled):
- `smart_search` uses semantic matching ("something refreshing" finds Sprite)
- `score_menu` ranks by cosine similarity to taste vector
- `get_recommendations` includes similar_product suggestions
If disabled, all features fall back to SQL keyword/frequency matching.
"""


@mcp.resource("rappi://info/store-types")
async def store_types_info() -> str:
    """How different store types work on Rappi."""
    return """# Rappi Store Types

| Type | Examples | How to Browse | Cart URL Type |
|------|----------|--------------|---------------|
| restaurant | El Corral, McDonald's | `get_restaurant_menu(store_id)` — returns categories with products | "restaurant" |
| turbo | Turbo convenience stores | `search_in_store(store_id, query)` — no static menu | "turbo" |
| larebaja | La Rebaja pharmacy | `search_in_store(store_id, query)` | varies |
| markets | Carulla, Exito | `search_in_store(store_id, query)` | varies |

Key differences:
- Restaurants have menu corridors (categories like "Hamburguesas", "Bebidas")
- Non-restaurant stores have thousands of SKUs, only searchable — no browse menu
- Store type appears in search results as `store_type` field
- The store type determines which URL path the cart/checkout API uses
"""


# --- Skill resources (auto-discovered by OpenClaw and other MCP clients) ---


@mcp.resource("rappi://skill/order-food")
async def skill_order_food() -> str:
    """Full ordering workflow — search, browse, cart, checkout, track. Use when the user wants to order food or anything from Rappi."""
    return """# Rappi Food Ordering

You are helping the user order from Rappi. All prices are in COP (Colombian Pesos), formatted with dot separators (e.g., $35.500).

## Available Tools (39 total)

### Discovery
- `explore_verticals()` — see all available store types (Restaurants, Turbo, Markets, Farmacia, Licores)
- `browse_restaurants(offset, limit)` — nearby restaurants
- `browse_stores(store_type, query)` — find stores by type (turbo, exito, carulla, olimpica, farmatodo)
- `search_restaurants(query)` — search all store types by keyword

### Store Browsing
- `get_restaurant_menu(store_id)` — full menu for restaurants
- `search_store_products(store_id, query)` — CPG product search (Turbo/markets — richer results)
- `search_in_store(store_id, query)` — search within any store
- `get_store_info(store_id)` — hours, charges, address
- `get_store_categories(store_id)` — browse aisles/categories
- `get_aisle_products(store_id, aisle_id)` — products in a category

### Cart & Checkout
- `add_to_cart(store_id, product_id, quantity, topping_ids, product_name, product_price)` — add item (pass name/price for Turbo/market stores)
- `view_cart()` — current cart
- `remove_from_cart(store_id, product_id)` — remove item
- `get_product_toppings(store_id, product_id)` — customization options
- `set_tip(tip_amount)` — set delivery tip (persists on server until order placed)
- `checkout(confirm)` — preview (confirm=false) or place (confirm=true)
- `get_payment_methods()` — available payment methods

### Order Tracking
- `get_active_orders()` — currently active orders
- `track_order(order_id)` — real-time state, ETA, driver position
- `get_order_detail(order_id)` — full order summary
- `get_order_breakdown(order_id)` — detailed cost breakdown
- `get_order_history(limit)` — past orders

### Account & Preferences
- `get_ordering_context()` — full state snapshot
- `get_credits_balance()` — Rappi credits/wallet
- `get_rappi_favorites()` — favorite stores
- `get_taste_profile()` — computed taste profile
- `get_recommendations()` — smart suggestions
- `get_preferences()` / `set_preference(key, value)` — dietary restrictions, default tip

## Workflow

### Step 1: Check Context
Call `get_ordering_context` FIRST to understand current state.

### Step 2: Find What They Want
- **See what's available**: `explore_verticals`
- **Specific food**: `search_restaurants(query)`
- **Browse restaurants**: `browse_restaurants`
- **Reorder**: `get_order_history` then `quick_reorder(order_id)`
- **Specific store type** (Turbo, Exito, etc.): `browse_stores(store_type, query)`

### Step 3: Show Menu / Products
- **Restaurants**: `get_restaurant_menu(store_id)`
- **Turbo/Markets**: `search_store_products(store_id, query)`
- **Any store**: `search_in_store(store_id, query)` as fallback

### Step 4: Handle Toppings
If `has_toppings=true`: call `get_product_toppings` BEFORE adding to cart. Categories with `min_required > 0` are MANDATORY.

### Step 5: Add to Cart
- **Restaurants**: `add_to_cart(store_id, product_id, quantity, topping_ids)`
- **Turbo/markets**: `add_to_cart(store_id, product_id, quantity, product_name="...", product_price=...)`

### Step 6: Checkout
1. Set tip: `set_tip(tip_amount)` — check preferences for default
2. Preview: `checkout(confirm=false)`
3. Show breakdown to user
4. ONLY after explicit confirmation: `checkout(confirm=true)`

### Step 7: Track
Call `track_order(order_id)` for real-time ETA and state.

## Error Handling
- "Token expired" (auto-refresh failed) → tell user to run `uv run rappi auth login`, then `uv run rappi auth push-railway` for the hosted server
- "Store unavailable" → suggest alternatives
- "Missing required toppings" → go back and ask
- "Product not found" (Turbo/markets) → pass product_name and product_price
"""


@mcp.resource("rappi://skill/search")
async def skill_search() -> str:
    """Quick search for restaurants, stores, or products on Rappi."""
    return """# Rappi Quick Search

Search for restaurants, stores, and products on Rappi. Lightweight search — no cart or checkout.

## Steps

1. Determine the search type:
   - **"What's available?"** → `explore_verticals` (all store types)
   - **Product search** (e.g., "pizza", "cerveza") → `search_restaurants(query)` (returns ALL store types)
   - **Store type browsing** (e.g., "Turbo stores", "Exito") → `browse_stores(store_type, query)`
   - **Restaurant browsing** → `browse_restaurants`
   - **Search within a store** → `search_store_products(store_id, query)` for CPG stores, or `search_in_store(store_id, query)`

2. Present results in a table: store name, type, ETA, delivery cost, matching products with prices.

3. For more products from a store:
   - Restaurants: `get_restaurant_menu(store_id)`
   - Turbo/markets: `search_store_products(store_id, query)` (richer results with brand info)
   - Hours/fees: `get_store_info(store_id)`

4. Additional info: `get_credits_balance()`, `get_rappi_favorites()`, `get_order_detail(order_id)`, `get_order_breakdown(order_id)`

5. Prices in COP — format as $35.500 with dot separators.

6. If user wants to order, guide them through the ordering flow.
"""


@mcp.resource("rappi://skill/reorder")
async def skill_reorder() -> str:
    """Reorder from Rappi order history."""
    return """# Rappi Quick Reorder

Help the user reorder from their order history.

## Steps

1. Call `get_order_history(limit=10)` to fetch recent orders.

2. Present past orders: store name, items, total price, date.

3. For order details: `get_order_detail(order_id)` or `get_order_breakdown(order_id)`.

4. Call `quick_reorder(order_id)` with the selected order — re-adds available items to cart.

5. Call `view_cart` to show what was added.

6. If some items failed, ask about replacements.

7. Set tip: `set_tip(tip_amount)` — check preferences for default.

8. Preview: `checkout(confirm=false)`. Mention `get_credits_balance()` if relevant.

9. After user confirms: `checkout(confirm=true)`. NEVER place without confirmation.

10. Offer to track with `track_order(order_id)`.

Prices in COP — format as $35.500.
"""


@mcp.resource("rappi://skill/suggest")
async def skill_suggest() -> str:
    """Smart food suggestions based on taste profile and ordering habits."""
    return """# Rappi Smart Suggestions

Help the user decide what to eat based on their taste profile.

## Steps

1. Call `get_recommendations(context)` with any user context (e.g., "lunch", "something cheap").

2. Present recommendations by type:

   **Your Usual** (type="usual"):
   - "You order [items] from [store] regularly — want the usual?"
   - Action: `quick_reorder` or `add_to_cart` for each item

   **Right Now** (type="time_based"):
   - "You usually order from [store] around this time"
   - Action: browse their menu with `get_restaurant_menu`

   **Try Something New** (type="new_store"):
   - "You haven't tried [store] yet"
   - Action: `search_restaurants` or `get_restaurant_menu`

3. For deeper insight: `get_taste_profile` shows categories, favorites, spending, time habits.

4. Additional context:
   - `explore_verticals()` — all store types
   - `get_credits_balance()` — budget awareness
   - `get_rappi_favorites()` — favorited stores
   - `browse_stores(store_type)` — specific store types

5. When user picks a recommendation, transition to ordering:
   - `quick_reorder` for "usual" picks
   - `get_restaurant_menu` + `add_to_cart` for restaurants
   - `search_store_products` + `add_to_cart(... product_name, product_price)` for Turbo/markets
   - Always set tip and confirm before checkout

Prices in COP — format as $35.500.
"""


# --- Helpers ---


async def _sync_address_coords(client: RappiClient) -> None:
    """Sync config coordinates from Rappi's active address.

    Ensures searches use the correct location even when the address
    was changed via the Rappi app/website, or when running on Railway
    without manually setting RAPPI_LAT/LNG.
    """
    try:
        addresses = await _list_addresses(client)
        active = next((a for a in addresses if a.active), None)
        if active and (active.lat != client.config.lat or active.lng != client.config.lng):
            client._config.lat = active.lat
            client._config.lng = active.lng
    except Exception:
        pass  # Best-effort — don't block ordering if address fetch fails


@asynccontextmanager
async def _client_synced():
    """Create a RappiClient with coordinates synced from Rappi's active address."""
    async with RappiClient() as client:
        await _sync_address_coords(client)
        yield client


@asynccontextmanager
async def _client_with_memory():
    """Create a RappiClient with MemoryManager and synced coordinates."""
    async with MemoryManager() as memory:
        async with RappiClient(memory=memory) as client:
            await _sync_address_coords(client)
            yield client, memory


def _find_product(store: StoreDetail, product_id: int) -> Product | None:
    """Find a product in a store's menu corridors."""
    for corridor in store.corridors:
        for p in corridor.products:
            if p.id == product_id:
                return p
    return None


def _validate_toppings(
    categories: list[ToppingCategory], selected_ids: list[int]
) -> dict:
    """Check that all required topping categories have enough selections."""
    missing = []
    for cat in categories:
        if cat.min_toppings_for_categories > 0:
            cat_topping_ids = {t.id for t in cat.toppings}
            selected_in_cat = [tid for tid in selected_ids if tid in cat_topping_ids]
            if len(selected_in_cat) < cat.min_toppings_for_categories:
                missing.append({
                    "category_id": cat.id,
                    "category_name": cat.description,
                    "min_required": cat.min_toppings_for_categories,
                    "selected_count": len(selected_in_cat),
                    "available_toppings": [
                        {"id": t.id, "name": t.description, "price": t.price}
                        for t in cat.toppings
                        if t.is_available
                    ],
                })
    return {"valid": len(missing) == 0, "missing": missing}


# --- Tools ---


@mcp.tool()
async def get_ordering_context() -> dict:
    """Get a snapshot of the user's current state: active address, cart contents, and active orders.

    When to use: Call this FIRST at the start of any ordering conversation to understand
    where the user is in the process before making suggestions.
    Next step: Based on the state, suggest searching for restaurants, continuing checkout, or tracking orders.
    """
    async with _client_with_memory() as (client, memory):
        profile = await _get_profile(client)
        prime = await _is_prime(client)
        addresses = await _list_addresses(client)
        active_addr = next((a for a in addresses if a.active), None)
        carts = await _get_carts(client)
        orders = await _get_orders(client)

        cart_summary = None
        if carts:
            all_items = []
            for cart in carts:
                for store in cart.stores:
                    for p in store.products:
                        all_items.append({"name": p.name, "quantity": p.units, "price": p.total})
            total = sum(c.sub_total for c in carts)
            if all_items:
                cart_summary = {"items": all_items, "total": total}

        # Memory context
        memory_summary = {}
        taste_summary = ""
        try:
            memory_summary = await memory.get_memory_summary()
            taste_summary = await memory.get_taste_summary()
        except Exception:
            pass

        return {
            "user": {"name": profile.name, "is_prime": prime.is_prime},
            "active_address": {
                "id": active_addr.id,
                "title": active_addr.title or active_addr.tag,
                "address": active_addr.address,
            } if active_addr else None,
            "cart": cart_summary,
            "memory": memory_summary,
            "taste_summary": taste_summary,
            "active_orders": [
                {
                    "id": o.id,
                    "store": o.store.name if o.store else None,
                    "state": o.state,
                    "eta": o.eta,
                }
                for o in orders.active_orders
            ],
        }


@mcp.tool()
async def auth_status() -> dict:
    """Check if the Rappi auth token is valid and return user profile.

    When to use: When the user asks about their account or you need to verify auth works.
    Next step: If authenticated, use get_ordering_context for full state.
    """
    async with _client_synced() as client:
        profile = await _get_profile(client)
        prime = await _is_prime(client)
        return {
            "authenticated": True,
            "name": profile.name,
            "email": profile.email,
            "is_prime": prime.is_prime,
            "token_expires_at": client.config.token_expires_at,
            "auto_refresh": bool(client.config.refresh_token),
        }


@mcp.tool()
async def list_delivery_addresses() -> dict:
    """List all saved delivery addresses. The active address determines which restaurants appear.

    When to use: When the user wants to check or switch their delivery location.
    Next step: Use set_delivery_address to switch, then search or browse restaurants.
    """
    async with _client_synced() as client:
        addresses = await _list_addresses(client)
        return {
            "addresses": [
                {
                    "id": a.id,
                    "title": a.title or a.tag,
                    "address": a.address,
                    "active": a.active,
                    "lat": a.lat,
                    "lng": a.lng,
                }
                for a in addresses
            ]
        }


@mcp.tool()
async def set_delivery_address(address_id: int) -> dict:
    """Set which address to deliver to. This affects which restaurants are available.

    When to use: When the user wants to switch delivery location.
    Next step: Search or browse restaurants for the new location.
    """
    async with _client_synced() as client:
        try:
            await _set_active_address(client, address_id)
        except Exception:
            # The PUT endpoint may return 404 — update coordinates from address list instead
            addresses = await _list_addresses(client)
            found = next((a for a in addresses if a.id == address_id), None)
            if not found:
                return {"error": f"Address {address_id} not found"}
            client._config.lat = found.lat
            client._config.lng = found.lng
        return {"success": True, "address_id": address_id}


@mcp.tool()
async def search_restaurants(query: str, max_stores: int = 10, max_products_per_store: int = 5) -> dict:
    """Search for restaurants, stores, and products matching a query.

    Returns ALL store types: restaurants, Turbo (convenience), markets, pharmacies, etc.
    The store_type field tells you what kind of store it is.

    max_stores: Maximum number of stores to return (default 10).
    max_products_per_store: Maximum products shown per store (default 5). Use get_restaurant_menu for full menus.

    When to use: The user asks for a specific food, product, or store by name.
    Next step: For restaurants (store_type="restaurant"), use get_restaurant_menu.
    For other stores (Turbo, markets), use search_in_store to find more products.
    Or use add_to_cart directly if the user wants a product from the results.
    """
    async with _client_synced() as client:
        stores = await _search(client, query)
        return {
            "stores": [
                {
                    "store_id": s.store_id,
                    "store_name": s.store_name,
                    "store_type": s.store_type,
                    "eta": s.eta,
                    "shipping_cost": s.shipping_cost,
                    "products": [
                        {
                            "product_id": p.product_id,
                            "name": p.name,
                            "price": p.price,
                            "in_stock": p.in_stock,
                            "has_toppings": p.has_toppings,
                        }
                        for p in s.products[:max_products_per_store]
                    ],
                }
                for s in stores[:max_stores]
            ]
        }


@mcp.tool()
async def search_in_store(store_id: int, query: str) -> dict:
    """Search for products within a specific store.

    This is the primary way to browse non-restaurant stores (Turbo, markets, pharmacies)
    which don't have a static menu. Results are grouped by category.

    When to use: When the user wants to find products in a specific Turbo, market, or other
    non-restaurant store. Also useful for searching within a restaurant.
    Next step: Use add_to_cart with the store_id and product_id from the results.
    """
    async with _client_synced() as client:
        corridors = await _search_store_products(client, store_id, query)
        return {
            "categories": [
                {
                    "name": c.name,
                    "products": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "description": p.description,
                            "price": p.price,
                            "in_stock": p.in_stock,
                        }
                        for p in c.products
                    ],
                }
                for c in corridors
            ]
        }


@mcp.tool()
async def browse_restaurants(offset: int = 0, limit: int = 20) -> dict:
    """Browse nearby restaurants without searching. Returns a paginated list sorted by relevance.

    When to use: The user wants to see what's available nearby without a specific query.
    Next step: Use get_restaurant_menu with a store_id to see the full menu.
    """
    async with _client_synced() as client:
        stores = await _get_catalog(client, offset=offset, limit=limit)
        return {
            "stores": [
                {
                    "store_id": s.store_id,
                    "name": s.name,
                    "rating": s.score,
                    "eta": s.eta,
                    "shipping_cost": s.shipping_cost,
                    "is_available": s.is_available,
                }
                for s in stores
            ]
        }


@mcp.tool()
async def browse_stores(store_type: str, query: str = "") -> dict:
    """Browse non-restaurant stores by type — Turbo, markets, pharmacies, liquor stores, etc.

    Uses unified search to discover stores. Pass the store type as query if no specific
    product query is given.

    store_type: The type to filter for — "turbo", "oxxo", "olimpica", "carulla",
    "exito", "larebaja", "farmatodo", "cervesia" (liquor), etc.
    query: Optional product to search for within those stores (e.g., "cerveza michelob").
    If empty, searches by store_type name to discover available stores.

    When to use: The user wants to find Turbo stores, markets, pharmacies, or any
    non-restaurant store. After finding a store, use search_in_store to browse products.
    """
    search_query = query or store_type
    async with _client_synced() as client:
        from rappi.services.search import search as _search_svc
        import unicodedata
        results = await _search_svc(client, search_query)

        def _normalize(s: str) -> str:
            return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")

        # Filter stores matching the requested type (accent-insensitive)
        filter_norm = _normalize(store_type)
        matched = []
        other = []
        for store in results:
            st = _normalize(store.store_type or "")
            sn = _normalize(store.store_name or "")
            if filter_norm in st or filter_norm in sn:
                matched.append(store)
            else:
                other.append(store)

        # Show matched stores first, then others that had product hits
        stores_to_show = matched or other
        return {
            "store_type_filter": store_type,
            "query": search_query,
            "matched_stores": len(matched),
            "total_stores": len(results),
            "stores": [
                {
                    "store_id": s.store_id,
                    "store_name": s.store_name,
                    "store_type": s.store_type,
                    "products": [
                        {
                            "product_id": p.product_id,
                            "name": p.name,
                            "price": p.price,
                            "in_stock": p.in_stock,
                        }
                        for p in (s.products or [])[:5]
                    ],
                }
                for s in stores_to_show[:15]
            ],
        }


@mcp.tool()
async def get_restaurant_menu(store_id: int, max_products_per_category: int = 10) -> dict:
    """Get the menu for a store, organized by category.

    Works for restaurants (returns menu corridors). For non-restaurant stores
    (Turbo, markets) this may return an empty menu — use search_in_store instead.

    max_products_per_category: Limit products per category (default 10) to keep
    response manageable. Large menus (McDonald's, etc.) can have 100+ items.

    When to use: After the user picks a restaurant from search/browse results.
    Next step: If a product has has_toppings=true, call get_product_toppings before adding to cart.
    If categories is empty, use search_in_store to find products.
    """
    async with _client_synced() as client:
        store = await _get_store_detail(client, store_id)
        result = {
            "store_id": store.store_id,
            "name": store.name,
            "store_type": store.effective_store_type,
            "status": store.status.status if store.status else "unknown",
            "note": "Product availability is confirmed when adding to cart. The menu shows all products regardless of real-time availability.",
            "categories": [
                {
                    "name": c.name,
                    "product_count": len(c.products),
                    "products": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "description": p.description,
                            "price": p.price,
                            "has_toppings": p.has_toppings,
                        }
                        for p in c.products[:max_products_per_category]
                    ],
                }
                for c in store.corridors
            ],
        }
        if not store.corridors and not store.is_restaurant:
            result["hint"] = (
                f"This is a {store.effective_store_type} store with no static menu. "
                "Use search_in_store to find products."
            )
        return result


@mcp.tool()
async def get_product_toppings(store_id: int, product_id: int) -> dict:
    """Get available toppings/customizations for a product.

    When to use: Before adding a product that has has_toppings=true. REQUIRED for products
    with mandatory topping categories (min_required > 0).
    Next step: Pass the selected topping IDs to add_to_cart.
    """
    async with _client_synced() as client:
        result = await _get_toppings(client, store_id, product_id)
        return {
            "categories": [
                {
                    "id": cat.id,
                    "description": cat.description,
                    "type": cat.topping_type_id,
                    "min_required": cat.min_toppings_for_categories,
                    "max_allowed": cat.max_toppings_for_categories,
                    "toppings": [
                        {
                            "id": t.id,
                            "description": t.description,
                            "price": t.price,
                            "available": t.is_available,
                        }
                        for t in cat.toppings
                    ],
                }
                for cat in result.categories
            ]
        }


@mcp.tool()
async def add_to_cart(
    store_id: int,
    product_id: int,
    quantity: int = 1,
    topping_ids: list[int] | None = None,
    product_name: str = "",
    product_price: float = 0,
) -> dict:
    """Add a product to the cart. Price and details are automatically fetched.

    For non-restaurant stores (Turbo, markets), provide product_name and product_price
    from the search results, since these stores don't have static menus to look up.

    IMPORTANT: If a product has required toppings (min_required > 0 in get_product_toppings),
    you MUST provide topping_ids for those categories. Otherwise this returns an error
    explaining which categories need selections.

    When to use: After the user picks a product and (if needed) customizations.
    Next step: Ask if they want to add more items, or use checkout to review/place the order.
    """
    async with _client_synced() as client:
        store = await _get_store_detail(client, store_id)
        product = _find_product(store, product_id)

        # For non-restaurant stores (Turbo, markets), products aren't in corridors —
        # check memory cache first, then search the store's products via API
        if not product and not store.is_restaurant:
            # Try memory cache (products seen in previous searches)
            if client.memory:
                try:
                    cached = await client.memory.products.get(f"{store_id}_{product_id}")
                    if cached:
                        product = Product(
                            id=product_id,
                            name=cached["name"],
                            price=cached["price"],
                            real_price=cached.get("real_price") or cached["price"],
                            in_stock=True,
                            has_toppings=False,
                        )
                except Exception:
                    pass

            # Fall back to building Product from provided name/price
            if not product and product_name and product_price > 0:
                product = Product(
                    id=product_id,
                    name=product_name,
                    price=product_price,
                    real_price=product_price,
                    in_stock=True,
                    has_toppings=False,
                )

            # Last resort: search the store for this product
            if not product:
                try:
                    search_stores = await _search(client, product_name or "product", store_id=store_id)
                    for ss in search_stores:
                        for sp in ss.products:
                            if sp.product_id == product_id:
                                product = Product(
                                    id=sp.product_id,
                                    name=sp.name,
                                    price=sp.price,
                                    real_price=sp.real_price or sp.price,
                                    image=sp.image,
                                    in_stock=sp.in_stock,
                                    has_toppings=sp.has_toppings,
                                    description=sp.presentation,
                                )
                                break
                        if product:
                            break
                except Exception:
                    pass

        if not product:
            return {"error": f"Product {product_id} not found in store {store_id}. For non-restaurant stores, use the product_id from search_restaurants or browse_stores results."}

        # Note: we do NOT block on product.in_stock here. The menu endpoint's
        # is_available flag is unreliable — it can be False even when the restaurant
        # is open and accepting orders. Let Rappi's cart API decide availability.

        # Validate toppings for products that require them
        selected_toppings = []
        if product.has_toppings:
            toppings_resp = await _get_toppings(client, store_id, product_id)

            # Check required toppings are provided
            validation = _validate_toppings(toppings_resp.categories, topping_ids or [])
            if not validation["valid"]:
                return {
                    "error": "Missing required toppings",
                    "missing_categories": validation["missing"],
                    "hint": "Call get_product_toppings first, then provide topping_ids for all required categories.",
                }

            # Resolve topping objects
            if topping_ids:
                topping_map = {}
                for cat in toppings_resp.categories:
                    for t in cat.toppings:
                        topping_map[t.id] = t
                selected_toppings = [topping_map[tid] for tid in topping_ids if tid in topping_map]

        # Use the correct store_type for the cart API endpoint
        store_type = store.effective_store_type or "restaurant"
        carts = await _add_to_cart(client, store_id, product, selected_toppings, quantity, store_type=store_type)
        total_items = sum(p.units for cart in carts for s in cart.stores for p in s.products)
        total_price = sum(cart.sub_total for cart in carts)
        return {
            "success": True,
            "product": product.name,
            "quantity": quantity,
            "unit_price": product.price,
            "cart_items": total_items,
            "cart_total": total_price,
        }


async def _detect_cart_store_type(client: RappiClient) -> str:
    """Detect the store_type from the current cart. Falls back to 'restaurant'."""
    try:
        carts = await _get_carts(client)
        if carts and carts[0].store_type:
            return carts[0].store_type
    except Exception:
        pass
    return "restaurant"


@mcp.tool()
async def view_cart() -> dict:
    """View current cart contents with items, quantities, prices, and totals.

    When to use: When the user wants to see what's in their cart before checking out.
    Next step: Use checkout(confirm=false) to preview the order, or remove_from_cart to remove items.
    """
    async with _client_synced() as client:
        carts = await _get_carts(client)
        if not carts:
            return {"empty": True, "stores": []}
        result_stores = []
        for cart in carts:
            for store in cart.stores:
                result_stores.append({
                    "store_id": store.id,
                    "store_name": store.name,
                    "store_type": cart.store_type,
                    "is_open": store.is_open,
                    "products": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "quantity": p.units,
                            "price": p.price,
                            "total": p.total,
                        }
                        for p in store.products
                    ],
                    "product_total": store.product_total,
                    "shipping": store.charge_total,
                    "total": store.total,
                })
        return {"empty": False, "stores": result_stores}


@mcp.tool()
async def remove_from_cart(store_id: int, product_id: int) -> dict:
    """Remove a product from the cart.

    Uses the same full-replacement PUT as add_to_cart — sends remaining items.

    When to use: When the user wants to remove a specific item.
    Next step: Use view_cart to show updated contents.
    """
    compound_id = make_compound_id(store_id, product_id)
    async with _client_synced() as client:
        store_type = await _detect_cart_store_type(client)
        carts = await _remove_from_cart(client, store_id, compound_id, store_type=store_type)
        total_items = sum(p.units for cart in carts for s in cart.stores for p in s.products) if carts else 0
        return {"success": True, "removed": compound_id, "cart_items": total_items, "store_type": store_type}


@mcp.tool()
async def checkout(confirm: bool = False) -> dict:
    """Preview checkout summary or place the order.

    IMPORTANT: Call set_tip BEFORE calling checkout to set the delivery tip.
    Do NOT pass tip here — use the dedicated set_tip tool. The tip persists on
    Rappi's server and will show in the preview automatically.

    Always call with confirm=False first to show the summary to the user.
    Only call with confirm=True after the user explicitly approves.
    NEVER place an order without explicit user confirmation.

    When to use: After the user is done adding items and wants to review or place the order.
    Next step: If preview, show the summary and ask user to confirm. If placed, use track_order to follow delivery.
    """
    async with _client_synced() as client:
        store_type = await _detect_cart_store_type(client)

        await _recalculate_cart(client, store_type=store_type)

        detail = await _get_checkout_detail(client, store_type=store_type)

        summary = []
        for s in detail.summary:
            items = [{"type": d.type, "label": strip_html(d.key), "value": strip_html(d.value)} for d in s.details]
            summary.append({
                "store": s.header.title if s.header else None,
                "items": items,
            })

        if not confirm:
            return {"preview": True, "store_type": store_type, "summary": summary, "return_key_available": bool(detail.return_key)}

        if not detail.return_key:
            return {"error": "No return_key — cannot place order. Cart may be empty or invalid."}

        # Safety check: warn on large orders (configurable via max_order_amount preference)
        try:
            carts = await _get_carts(client)
            order_total = sum(c.sub_total for c in carts)
            max_amount = 500_000  # Default max: $500,000 COP
            if client.memory:
                try:
                    custom_max = await client.memory.preferences.get("max_order_amount")
                    if custom_max:
                        max_amount = float(custom_max)
                except Exception:
                    pass
            if order_total > max_amount:
                return {
                    "error": f"Order total (${order_total:,.0f}) exceeds safety limit (${max_amount:,.0f}). "
                    "Set a higher limit with set_preference('max_order_amount', '1000000') or confirm in the Rappi app.",
                }
        except Exception:
            pass

        result = await _place_order(client, detail.return_key, store_type=store_type)
        return {"placed": True, "store_type": store_type, "summary": summary, "result": result}


@mcp.tool()
async def get_order_status() -> dict:
    """Get active and cancelled orders with their current status.

    When to use: After placing an order, or when the user asks about their order status.
    Order states: created -> in_store -> on_the_way -> delivered (or cancelled).
    """
    async with _client_synced() as client:
        result = await _get_orders(client)
        return {
            "active_orders": [
                {
                    "id": o.id,
                    "store": o.store.name if o.store else None,
                    "state": o.state,
                    "total": o.total,
                    "eta": o.eta,
                    "tip": o.tip,
                    "can_cancel": o.can_be_cancel,
                }
                for o in result.active_orders
            ],
            "cancelled_orders": [
                {"id": o.id, "store": o.store.name if o.store else None}
                for o in result.cancel_orders
            ],
        }


# --- Memory tools ---


@mcp.tool()
async def get_order_history(
    limit: int = 10,
    refresh: bool = True,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Get past completed orders — store, total, date, items — from the Rappi API.

    Pulls real order history from Rappi's account/orders endpoint (paginated,
    newest first) and caches each order into local memory. Falls back to the
    local cache if the API is unavailable.

    When to use: User asks "what did I order last time?", "show my order history",
    or wants to analyze past spending.
    Next step: Use quick_reorder to re-add items, or get_spending_summary to
    break spend down by store type.

    Args:
        limit: Max orders to return (ignored when since/until are set — those
            return the full matching window).
        refresh: If True, fetch fresh from the API; if False, read local cache only.
        since: Only include orders on/after this ISO date (YYYY-MM-DD).
        until: Only include orders before this ISO date (YYYY-MM-DD).
    """
    async with MemoryManager() as memory:
        if refresh:
            try:
                async with _client_synced() as client:
                    fetched = await _fetch_order_history(
                        client, since=since, until=until,
                        max_pages=200 if (since or until) else 20,
                    )
                # Cache into local memory so offline/cache reads stay warm.
                for o in fetched:
                    try:
                        await memory.orders.save(
                            order_id=o["id"],
                            store_id=o.get("store_id") or 0,
                            store_name=o.get("store_name"),
                            store_type=o.get("store_type"),
                            total=o.get("total") or 0.0,
                            tip=0.0,
                            state=o.get("state"),
                            placed_at=o.get("created_at") or "",
                        )
                    except Exception:
                        pass
                orders = fetched if (since or until) else fetched[:limit]
                return {"orders": orders, "count": len(orders), "source": "api"}
            except Exception as e:
                # Fall through to local cache on any API/auth error.
                api_error = str(e)
            else:
                api_error = None
        else:
            api_error = None

        # Local-cache path (refresh=False, or API failed).
        records = await memory.orders.list_recent(limit=limit if not (since or until) else 1000)
        orders = []
        for o in records:
            created = (o.placed_at or "")[:10]
            if since and created and created < since:
                continue
            if until and created and created >= until:
                continue
            orders.append({
                "id": o.id,
                "store_name": o.store_name,
                "store_type": o.store_type,
                "total": o.total,
                "tip": o.tip,
                "state": o.state,
                "created_at": o.placed_at,
                "product_names": [i.get("name") for i in (o.items or [])],
            })
        result = {"orders": orders, "count": len(orders), "source": "cache"}
        if api_error:
            result["api_error"] = api_error
        return result


@mcp.tool()
async def get_spending_summary(since: str | None = None, until: str | None = None) -> dict:
    """Summarize Rappi spending by store type over a date window.

    Pulls completed order history from the API and aggregates totals and order
    counts per store_type (restaurant, turbo, market, pharmacy, etc.), plus a
    grand total. Amounts are in the account's local currency (COP for Colombia).

    When to use: User asks "how much am I spending on Rappi?", "where does my
    Rappi money go?", "break down my delivery spend", or wants a spending report.

    Args:
        since: Only include orders on/after this ISO date (YYYY-MM-DD).
        until: Only include orders before this ISO date (YYYY-MM-DD).
    """
    async with _client_synced() as client:
        orders = await _fetch_order_history(
            client, since=since, until=until, max_pages=200
        )

    by_type: dict[str, dict] = {}
    grand_total = 0.0
    for o in orders:
        stype = o.get("store_type") or "unknown"
        bucket = by_type.setdefault(stype, {"store_type": stype, "orders": 0, "total": 0.0})
        bucket["orders"] += 1
        bucket["total"] += o.get("total") or 0.0
        grand_total += o.get("total") or 0.0

    breakdown = sorted(by_type.values(), key=lambda b: b["total"], reverse=True)
    for b in breakdown:
        b["total"] = round(b["total"], 2)
        b["pct"] = round(100 * b["total"] / grand_total, 1) if grand_total else 0.0
        b["avg_ticket"] = round(b["total"] / b["orders"], 2) if b["orders"] else 0.0

    return {
        "since": since,
        "until": until,
        "order_count": len(orders),
        "grand_total": round(grand_total, 2),
        "avg_ticket": round(grand_total / len(orders), 2) if orders else 0.0,
        "by_store_type": breakdown,
    }


@mcp.tool()
async def get_favorites() -> dict:
    """Get the user's favorite stores.

    When to use: User asks for favorites or wants to quickly pick a restaurant.
    Next step: Use get_restaurant_menu or search_in_store for a favorite.
    """
    async with MemoryManager() as memory:
        store_ids = await memory.preferences.get_favorite_store_ids()
        stores = []
        for sid in store_ids:
            cached = await memory.stores.get(sid, ttl_hours=99999)
            stores.append({
                "store_id": sid,
                "name": cached["name"] if cached else None,
                "store_type": cached.get("store_type") if cached else None,
            })
        return {"favorites": stores}


@mcp.tool()
async def add_favorite(store_id: int) -> dict:
    """Mark a store as a favorite for quick access later.

    When to use: User says "save this restaurant" or "add to favorites".
    """
    async with MemoryManager() as memory:
        await memory.preferences.add_favorite_store(store_id)
        return {"success": True, "store_id": store_id}


@mcp.tool()
async def remove_favorite(store_id: int) -> dict:
    """Remove a store from favorites."""
    async with MemoryManager() as memory:
        await memory.preferences.remove_favorite_store(store_id)
        return {"success": True, "store_id": store_id}


@mcp.tool()
async def quick_reorder(order_id: int) -> dict:
    """Re-add all items from a past order to the cart.

    When to use: User says "order the same thing as last time" or "reorder".
    Next step: Use checkout to review and place the order.
    """
    async with _client_with_memory() as (client, memory):
        order = await memory.orders.get_by_id(order_id)
        if not order:
            return {"error": f"Order {order_id} not found in history"}

        if not order.items:
            return {"error": "No items found for this order"}

        # Get store detail to fetch current prices
        store = await _get_store_detail(client, order.store_id)

        added = []
        failed = []
        for item in order.items:
            product = _find_product(store, int(item["product_id"]))
            if product and product.in_stock:
                try:
                    await _add_to_cart(
                        client, order.store_id, product, [],
                        item.get("quantity", 1),
                        store_type=store.effective_store_type,
                    )
                    added.append(item["name"])
                except Exception:
                    failed.append(item["name"])
            else:
                failed.append(f"{item['name']} (unavailable)")

        return {
            "success": True,
            "store": order.store_name,
            "added": added,
            "failed": failed,
        }


@mcp.tool()
async def get_preferences() -> dict:
    """Get user preferences: dietary restrictions, allergies, default tip.

    When to use: When making food recommendations or setting up checkout.
    """
    async with MemoryManager() as memory:
        return await memory.preferences.get_all()


@mcp.tool()
async def set_preference(key: str, value: str) -> dict:
    """Set a user preference.

    Keys: default_tip (int), dietary_restrictions (comma-separated), allergies (comma-separated).
    When to use: User says "I'm vegetarian", "always tip 5000", or "I'm allergic to peanuts".
    """
    async with MemoryManager() as memory:
        if key == "default_tip":
            await memory.preferences.set_default_tip(int(value))
        elif key == "dietary_restrictions":
            await memory.preferences.set_dietary_restrictions([v.strip() for v in value.split(",")])
        elif key == "allergies":
            await memory.preferences.set_allergies([v.strip() for v in value.split(",")])
        else:
            await memory.preferences.set(key, value)
        return {"success": True, "key": key, "value": value}


@mcp.tool()
async def smart_search(query: str, limit: int = 10) -> dict:
    """Search across cached products, order history, and favorites using
    text matching — or semantic search if embeddings are enabled.

    When to use: User asks vague things like "that spicy chicken thing from last week"
    or "something sweet". Falls back to keyword search if embeddings are not configured.
    """
    async with MemoryManager() as memory:
        results = await memory.smart_search(query, limit=limit)
        return {"results": results}


@mcp.tool()
async def get_taste_profile() -> dict:
    """Get the user's computed taste profile based on their entire order history.

    Returns: category preferences (what cuisines they like), store type preferences,
    price range, time-of-day patterns, topping preferences, top products/stores,
    spending summary, dietary restrictions, and allergies.

    When to use: User asks "what do I usually order?", "what are my food habits?",
    or when you need context to make personalized food recommendations.
    Next step: Use this to inform search queries and restaurant suggestions.
    """
    async with MemoryManager() as memory:
        profile = await memory.get_taste_profile()
        return profile.model_dump()


@mcp.tool()
async def get_recommendations(context: str | None = None) -> dict:
    """Get smart food recommendations based on the user's taste profile, current time,
    and order history.

    Returns scored recommendations: "the usual" orders, time-appropriate stores,
    stores they haven't tried yet.

    Args:
        context: Optional hint like "lunch", "quick snack", "something cheap".

    When to use: User asks "what should I eat?", "suggest something", "I'm hungry",
    or at the start of a conversation to proactively offer relevant options.
    Next step: Use the store_id from a recommendation with get_restaurant_menu or quick_reorder.
    """
    async with MemoryManager() as memory:
        ctx = {"raw_context": context} if context else None
        result = await memory.get_recommendations(ctx)
        return result.model_dump()


@mcp.tool()
async def score_menu(store_id: int) -> dict:
    """Score a restaurant's menu items by how well they match the user's taste.

    Uses embeddings (if enabled) to compute similarity between each product and
    the user's taste vector. Falls back to order-frequency scoring without embeddings.
    Items the user has ordered before or that match their taste rank highest.

    When to use: After fetching a menu with get_restaurant_menu, call this to help
    the user pick. "Based on your taste, I'd recommend these items."
    """
    async with _client_with_memory() as (client, memory):
        store = await _get_store_detail(client, store_id)
        all_products = [p for c in store.corridors for p in c.products if p.in_stock]

        if not all_products:
            return {"store_id": store_id, "scored_items": [], "note": "No available products"}

        scored = await memory.intelligence.score_menu_items(store_id, all_products)
        return {
            "store_id": store_id,
            "store_name": store.name,
            "scored_items": [
                {
                    "product_id": getattr(s["product"], "id", 0),
                    "name": getattr(s["product"], "name", ""),
                    "price": getattr(s["product"], "price", 0),
                    "match_score": s["match_score"],
                    "scoring_method": s["source"],
                }
                for s in scored[:15]
            ],
        }


# --- Discovery & Browsing Tools ---


@mcp.tool()
async def explore_verticals() -> dict:
    """Show all available store types in your area — Restaurants, Turbo, Markets, Licores, Farmacia, etc.

    When to use: User asks "what can I order?", "what's available?", or wants to see all options.
    Next step: Use browse_restaurants, browse_stores, or get_store_categories depending on what they pick.
    """
    async with _client_synced() as client:
        verticals = await _get_home_verticals(client)
        return {
            "verticals": [
                {
                    "id": v.get("id"),
                    "name": v.get("title", v.get("description", "Unknown")),
                    "description": v.get("description", ""),
                }
                for v in verticals[:20]
                if isinstance(v, dict)
            ],
        }


@mcp.tool()
async def get_store_categories(store_id: int, store_type: str = "turbo", parent_store_type: str = "turbo_home") -> dict:
    """Browse a store's aisles/categories — see what sections are available (Snacks, Bebidas, Aseo, etc.).

    store_type: "turbo", "market", "super", etc.
    parent_store_type: "turbo_home" for Turbo, "market" for markets.

    When to use: User wants to browse a non-restaurant store by category instead of searching.
    Next step: Use get_aisle_products with an aisle_id to see products in a category.
    """
    async with _client_synced() as client:
        aisles = await _get_store_aisles(client, store_id, store_type, parent_store_type)
        return {
            "store_id": store_id,
            "categories": [
                {
                    "id": a.get("id", a.get("aisle_id", a.get("corridor_id"))),
                    "name": a.get("name", a.get("description", a.get("title", "Unknown"))),
                    "image": a.get("image", a.get("icon")),
                    "product_count": a.get("product_count", a.get("products_count", 0)),
                }
                for a in aisles[:30]
                if isinstance(a, dict)
            ],
        }


@mcp.tool()
async def get_aisle_products(
    store_id: int, aisle_id: str, store_type: str = "turbo",
    parent_store_type: str = "turbo_home", max_products: int = 30,
) -> dict:
    """Get products within a specific store aisle/category.

    aisle_id: The category ID from get_store_categories results.

    When to use: User picked a category and wants to see what's in it.
    Next step: Use add_to_cart with product_id, product_name, product_price to add items.
    """
    async with _client_synced() as client:
        products = await _get_aisle_products(client, store_id, aisle_id, store_type, parent_store_type)
        return {
            "store_id": store_id,
            "aisle_id": aisle_id,
            "products": [
                {
                    "product_id": p.get("id", p.get("product_id")),
                    "name": p.get("name", p.get("product_name", "Unknown")),
                    "price": p.get("price", p.get("real_price", 0)),
                    "real_price": p.get("real_price", p.get("price", 0)),
                    "in_stock": p.get("in_stock", p.get("is_available", True)),
                    "image": p.get("image"),
                    "brand": p.get("brand", p.get("brand_name")),
                    "quantity_label": p.get("quantity", p.get("presentation")),
                }
                for p in products[:max_products]
                if isinstance(p, dict)
            ],
        }


@mcp.tool()
async def get_store_info(store_id: int, parent_store_type: str = "turbo_home") -> dict:
    """Get store details — hours, delivery charges, address, status.

    When to use: User wants to know if a store is open, delivery fee, or minimum order.
    """
    async with _client_synced() as client:
        info = await _get_store_info(client, store_id, parent_store_type)
        return {"store_id": store_id, "info": info}


@mcp.tool()
async def search_store_products(store_id: int, query: str, max_results: int = 20) -> dict:
    """Search for products within a CPG store (Turbo, markets) — richer than unified search.

    Returns detailed product info including brand, attributes, and category.

    When to use: User wants to find a specific product in a Turbo or market store.
    Next step: Use add_to_cart with product_id, product_name, product_price.
    """
    async with _client_synced() as client:
        products = await _search_cpg_products(client, store_id, query, limit=max_results)
        return {
            "store_id": store_id,
            "query": query,
            "products": [
                {
                    "product_id": p.get("id", p.get("product_id")),
                    "name": p.get("name", p.get("product_name", "Unknown")),
                    "price": p.get("price", 0),
                    "real_price": p.get("real_price", p.get("price", 0)),
                    "in_stock": p.get("in_stock", p.get("is_available", True)),
                    "brand": p.get("brand", p.get("brand_name")),
                    "category": p.get("category_name", p.get("category")),
                    "quantity_label": p.get("quantity", p.get("presentation")),
                    "has_toppings": p.get("has_toppings", False),
                }
                for p in products[:max_results]
                if isinstance(p, dict)
            ],
        }


# --- Order Tracking Tools ---


@mcp.tool()
async def get_order_detail(order_id: int) -> dict:
    """Get full order summary — products, totals, store, delivery address.

    When to use: User wants to see details of a specific past or active order.
    """
    async with _client_synced() as client:
        data = await _get_order_resume(client, order_id)
        return {"order_id": order_id, "detail": data}


@mcp.tool()
async def track_order(order_id: int) -> dict:
    """Track an active order — real-time state, ETA, driver position, timeline.

    When to use: User wants to know where their order is, when it arrives, or who the driver is.
    """
    async with _client_synced() as client:
        state = await _get_order_realtime_state(client, order_id)
        return {
            "order_id": order_id,
            "state": state.get("flow_key", state.get("state", "unknown")),
            "eta": state.get("eta", state.get("estimated_time")),
            "driver": {
                "name": state.get("driver_name", state.get("shopper_name")),
                "lat": state.get("driver_lat"),
                "lng": state.get("driver_lng"),
            } if state.get("driver_name") or state.get("shopper_name") else None,
            "timeline": state.get("timeline", state.get("steps", [])),
            "raw": {k: v for k, v in state.items() if k not in ("timeline", "steps")} if not state.get("flow_key") else None,
        }


@mcp.tool()
async def get_order_breakdown(order_id: int) -> dict:
    """Get detailed cost breakdown for an order — subtotal, delivery, service fee, tip, discounts.

    When to use: User asks "how much did I pay?", "what were the fees?", or wants receipt details.
    """
    async with _client_synced() as client:
        data = await _get_order_cost_breakdown(client, order_id)
        return {"order_id": order_id, "breakdown": data}


# --- Payment & Account Tools ---


@mcp.tool()
async def get_tip_suggestions() -> dict:
    """Get suggested tip amounts for the current cart.

    Returns suggested amounts with percentage labels and absolute COP values.
    One suggestion is marked as default.

    When to use: Before setting a tip, to show the user their options.
    """
    from rappi.services.checkout import get_tip_suggestions as _get_tip_suggestions
    async with _client_synced() as client:
        data = await _get_tip_suggestions(client)
        tips = data.get("tips", [])
        return {
            "suggestions": [
                {
                    "label": t.get("key", ""),
                    "amount": t.get("price", 0),
                    "is_default": t.get("default", False),
                }
                for t in tips
            ],
            "min_tip": data.get("min_tip_accepted", 0),
            "max_tip": data.get("max_value", 0),
        }


@mcp.tool()
async def set_tip(tip_amount: int) -> dict:
    """Set the delivery tip amount. Recalculates cart totals automatically.

    tip_amount: Absolute amount in COP (e.g., 2000 = $2.000 COP). NOT a percentage.
    Use 0 to remove tip.

    When to use: Before checkout, or when the user wants to change the tip.
    The tip persists on Rappi's server and will show in the checkout preview.
    Call get_tip_suggestions first to show recommended amounts.
    """
    async with _client_synced() as client:
        store_type = await _detect_cart_store_type(client)
        await _set_tip(client, tip_amount, store_type=store_type)
        return {"success": True, "tip_amount": tip_amount, "store_type": store_type}


@mcp.tool()
async def get_payment_methods() -> dict:
    """Get available payment methods and saved cards.

    When to use: User wants to see their payment options or check which card is active.
    """
    async with _client_synced() as client:
        data = await _get_payment_methods(client)
        return {"payment_methods": data}


@mcp.tool()
async def get_rappi_favorites() -> dict:
    """Get favorite stores from Rappi's API — shows stores the user has favorited in the app.

    When to use: User asks about their favorite stores or wants to order from a saved place.
    Next step: Use get_restaurant_menu or search_store_products on the store.
    """
    async with _client_synced() as client:
        stores = await _get_favorite_stores_api(client)
        return {
            "favorites": [
                {
                    "store_id": s.get("store_id", s.get("id")),
                    "name": s.get("name", s.get("store_name", "Unknown")),
                    "store_type": s.get("store_type"),
                    "logo": s.get("logo", s.get("image")),
                }
                for s in stores[:20]
                if isinstance(s, dict)
            ],
        }


@mcp.tool()
async def get_credits_balance() -> dict:
    """Get Rappi credits/wallet balance.

    When to use: User asks "how much credit do I have?" or before checkout to check balance.
    """
    async with _client_synced() as client:
        data = await _get_rappi_credits(client)
        return {"credits": data}


@mcp.tool()
async def get_active_orders() -> dict:
    """Get currently active orders (being prepared, on the way, etc.).

    When to use: User asks "where is my order?" or "do I have any orders?".
    Next step: Use track_order with the order_id for real-time tracking.
    """
    async with _client_synced() as client:
        orders = await _get_active_orders_v3(client)
        return {
            "orders": [
                {
                    "order_id": o.get("id", o.get("order_id")),
                    "store_name": o.get("store_name", o.get("store", {}).get("name") if isinstance(o.get("store"), dict) else None),
                    "state": o.get("state", o.get("status")),
                    "total": o.get("total", o.get("total_value")),
                    "eta": o.get("eta", o.get("estimated_time")),
                }
                for o in orders[:10]
                if isinstance(o, dict)
            ],
        }


# --- Token keep-alive (HTTP transports only) ---

KEEPALIVE_INTERVAL_S = 6 * 60 * 60
_logger = logging.getLogger("rappi.mcp")


async def keepalive_tick() -> str:
    """Refresh the Rappi token if it is due. Never raises; returns a log-safe summary."""
    try:
        config_manager = ConfigManager()
        config = config_manager.load()
        if not config.refresh_token:
            return "skipped: no refresh token"
        if not needs_proactive_refresh(config):
            return f"not due, expires {config.token_expires_at}"
        fresh = await refresh_tokens(config_manager, failed_token=config.token)
        return f"refreshed, expires {fresh.token_expires_at}"
    except RefreshFailedError as e:
        _logger.warning("keepalive refresh failed: %s", e)
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — the keep-alive must never crash the server
        _logger.warning("keepalive failed: %s", type(e).__name__)
        return f"error: {type(e).__name__}"


MISSING_VOLUME_WARNING = (
    "[rappi-mcp] WARNING: RAPPI_REFRESH_TOKEN set without RAPPI_CONFIG_DIR — refreshed tokens "
    "will be lost on restart; mount a volume (README § Auth runbook)"
)


def warn_if_no_config_volume() -> bool:
    """Warn (never refuse) when refreshed tokens would live only in the container's ~/.rappi."""
    if os.environ.get("RAPPI_REFRESH_TOKEN") and not os.environ.get("RAPPI_CONFIG_DIR"):
        print(MISSING_VOLUME_WARNING, file=sys.stderr, flush=True)
        return True
    return False


async def _keepalive_loop(interval: float = KEEPALIVE_INTERVAL_S) -> None:
    while True:
        print(f"[rappi-mcp] token keepalive: {await keepalive_tick()}", file=sys.stderr, flush=True)
        await asyncio.sleep(interval)


def build_http_app():
    """Starlette app serving streamable HTTP (/mcp), legacy SSE (/sse), /health, and the token keep-alive."""
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    def health(_request):
        return PlainTextResponse("ok")

    # Serve BOTH transports: streamable HTTP at /mcp (modern, what claude.ai
    # prefers) and legacy SSE at /sse + /messages (existing connectors).
    # The streamable app owns the lifespan (session manager), so it is the
    # root app and the SSE + health routes are grafted onto its router.
    app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()
    app.router.routes.extend(sse_app.routes)
    app.router.routes.append(Route("/health", health))

    # Wrap (not replace) the session-manager lifespan to run the keep-alive beside it.
    session_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(starlette_app):
        warn_if_no_config_volume()
        task = asyncio.create_task(_keepalive_loop())
        try:
            async with session_lifespan(starlette_app) as state:
                yield state
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.router.lifespan_context = lifespan
    return app


def main():
    if _transport in ("sse", "streamable-http", "http"):
        import uvicorn

        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8000")))
        app = build_http_app()

        print(f"[rappi-mcp] Starting {_transport} on {host}:{port}", flush=True)
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run(transport=_transport)
