# Rappi API Endpoints — Full Reference

Captured from browser DevTools on rappi.com.co (April 2026).

## Currently Implemented

| Endpoint | Method | Used In |
|----------|--------|---------|
| `/ms/application-user/auth` | GET | auth_status |
| `/api/ms/rappi-prime/is-prime` | GET | auth_status |
| `/api/ms/users-address/addresses` | GET | list_addresses, _sync_address_coords |
| `/api/ms/users-address/addresses/{id}/active` | PUT | set_delivery_address |
| `/api/pns-global-search-api/v1/unified-search` | POST | search_restaurants, browse_stores |
| `/api/restaurant-bus/stores/catalog-paged/home` | POST | browse_restaurants |
| `/api/web-gateway/web/stores-router/id/{store_id}/` | GET | get_restaurant_menu, add_to_cart |
| `/api/restaurant-bus/store/{store_id}/menu` | GET | get_restaurant_menu |
| `/api/web-gateway/web/restaurants-bus/products/toppings/{store_id}/{product_id}/` | GET | get_product_toppings |
| `/api/ms/shopping-cart/v2/{store_type}/store` | PUT | add_to_cart |
| `/api/ms/shopping-cart/v1/all/get` | POST | view_cart |
| `/api/ms/shopping-cart/v2/{store_type}/product/{compound_id}` | DELETE | remove_from_cart |
| `/api/ms/shopping-cart/v1/{store_type}/recalculate` | POST | checkout |
| `/api/ms/shopping-cart/v1/{store_type}/checkout/detail` | GET | checkout |
| `/api/ms/shopping-cart/v1/{store_type}/tip` | PUT | checkout |
| `/api/ms/shopping-cart-proxy/{store_type}/checkout` | POST | place_order |
| `/api/user-order-home/orders` | GET | get_order_status (active + cancelled) |
| `https://v2.grability.rappi.com/api/orders/history-user?page=N` | GET | get_order_history, get_spending_summary — **completed-order history**, paginated newest-first, on the **v2** gateway host (not `services`); 429-rate-limits rapid paging |

## Not Yet Implemented

### Homepage & Verticals

| Endpoint | Method | Purpose | Priority |
|----------|--------|---------|----------|
| `/home/high/?lat=...&lng=...&source=web` | GET | All verticals in area (restaurant, turbo_home, market, licores, farmacia) | HIGH |
| `/api/web-gateway/web/stores-router/available/principal/?lat=...&lng=...&view=web` | GET | Full store type hierarchy with suboptions and store IDs | HIGH |

### Store Browsing (Dynamic Content)

All via `POST /api/web-gateway/web/dynamic/context/content/` with different context values:

| Context | Purpose | Key State Params | Priority |
|---------|---------|------------------|----------|
| `store_home` | Store homepage (aisles, favorites, offers) | `store_type: "turbo"`, `parent_store_type: "turbo_home"`, `stores: [store_id]` | HIGH |
| `aisles_tree` | Full aisle/category tree | same as store_home | HIGH |
| `sub_aisles` | Products within an aisle | adds `aisle_id`, `parent_id` | HIGH |
| `store_information` | Store info, hours, charges | `parent_store_type: "turbo_home"`, `stores: [store_id]` | MEDIUM |
| `cpgs_landing` | Market landing categories | `parent_store_type: "market"` | MEDIUM |
| `on_top_stores` | Add to active order | `order_id`, `store_type: "turbo"` | LOW |

Known aisle IDs (Turbo): 3527, 8585, 3501, 3493, 3554

### In-Store Product Search (CPG-specific)

| Endpoint | Method | Body | Purpose | Priority |
|----------|--------|------|---------|----------|
| `/api/cpgs/search/v2/store/{store_id}/products` | POST | `{"from": 0, "query": "cerveza", "size": 40}` | Full product search with attributes (ABV, origin, etc.) | HIGH |
| `/api/pns-global-search-api/v1/unified-suggestions` | POST | `{"keyword": "cerveza", "suggester_type": "local_cpgs", "parent_store_type": "turbo"}` | Search autocomplete | LOW |

### Order Tracking

| Endpoint | Method | Purpose | Priority |
|----------|--------|---------|----------|
| `/order-resume/fully/{order_id}` | GET | Full order summary (products, totals, store, address) | HIGH |
| `/api/ms/user-order-state/auth/{order_id}` | GET | Real-time state: flow_key, timeline, ETA, map positions | HIGH |
| `/api/support-order-cost/orders/{order_id}/products` | GET | Order products detail | MEDIUM |
| `/api/support-order-cost/orders/{order_id}/costs-and-discounts` | GET | Full cost breakdown | MEDIUM |
| `/realtime-interface/orders/{order_id}/chats` | GET | Chat messages with driver/shopper | LOW |
| `/realtime-interface/databases` | GET | Firebase URLs for real-time updates | LOW |
| `/realtime-interface/firebase/login` | GET | Firebase auth token | LOW |

### Payment

| Endpoint | Method | Purpose | Priority |
|----------|--------|---------|----------|
| `/api/ms/payment-method/default-payment-method` | GET | Available payment methods + saved cards | MEDIUM |
| `/api/ms/shopping-cart/v1/{store_type}/payment-method` | PUT | Set payment method before checkout | MEDIUM |
| `/payments-conciliator/user-debts/v3` | GET | Check outstanding debts | LOW |

### User/Account

| Endpoint | Method | Purpose | Priority |
|----------|--------|---------|----------|
| `/api/pns-global-search-api/v1/unified-favorite-stores` | POST | Get favorite stores (better than current impl) | MEDIUM |
| `/api/ms/rappi-credits-mongo/` | GET | Rappi credits balance | MEDIUM |
| `/api/user-order-home/v3/orders` | GET | Active orders widget (newer version) | MEDIUM |
| `/api/dynamic/context/validate-user` | POST | Age verification for alcohol | LOW |
| `/api/ms/shopping-cart/v1/{store_type}/change-address` | POST | Change delivery address for active cart | LOW |
| `/api/cpgs-cart/store_type/turbo` | GET | Cart config for turbo | LOW |

## Key Patterns

- **store_type in URL path**: `/v1/turbo/...`, `/v2/turbo/...`, `/v1/restaurant/...`, `/v1/market/...`
- **Product ID format**: `{store_id}_{product_id}` (compound ID)
- **Cart ID format**: `{user_id}_{store_type}`
- **Dynamic content**: Single endpoint `/api/web-gateway/web/dynamic/context/content/` with `context` param drives all store browsing
- **CPG stores** (Turbo, markets): Use `/api/cpgs/...` endpoints, not restaurant endpoints
- **Verticals hierarchy**: `turbo` → parent `turbo_home`, `market` → parent `market`
