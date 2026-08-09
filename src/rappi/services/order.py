"""Order tracking services."""

import asyncio

from rappi.client import RappiAPIError, RappiClient
from rappi.constants import V2_BASE_URL, Endpoints
from rappi.models.order import OrdersResponse


async def get_orders(client: RappiClient) -> OrdersResponse:
    """Get active and cancelled orders."""
    data = await client.get(Endpoints.GET_ORDERS)
    return OrdersResponse(**data)


def _normalize_history_order(o: dict) -> dict:
    """Flatten one raw history order into a stable, MCP-friendly shape."""
    store = o.get("store") or {}
    products = o.get("products") or []
    total_raw = o.get("total_value")
    try:
        total = float(total_raw) if total_raw is not None else 0.0
    except (TypeError, ValueError):
        total = 0.0
    return {
        "id": o.get("id"),
        "state": o.get("state"),
        "total": total,
        "created_at": o.get("created_at"),
        "closed_at": o.get("closed_at"),
        "store_id": store.get("store_id") or store.get("id"),
        "store_name": store.get("name"),
        "store_type": store.get("store_type"),
        "brand_name": store.get("brand_name") or None,
        "product_names": [p.get("name") for p in products if p.get("name")],
        "n_products": len(products),
    }


async def get_order_history_page(
    client: RappiClient, page: int = 1, max_retries: int = 4
) -> dict:
    """Fetch one page of completed-order history from the V2 gateway.

    Retries with exponential backoff on HTTP 429 (the gateway rate-limits
    rapid pagination). Returns the raw paginated payload: total, per_page,
    current_page, last_page, next_page_url, data[...].
    """
    url = f"{V2_BASE_URL}{Endpoints.ORDER_HISTORY}"
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return await client.get(url, params={"page": page})
        except RappiAPIError as e:
            if e.status_code == 429 and attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    raise RappiAPIError(429, "rate limited after retries")


async def fetch_order_history(
    client: RappiClient,
    since: str | None = None,
    until: str | None = None,
    max_pages: int = 100,
) -> list[dict]:
    """Fetch and normalize completed orders, newest first, across pages.

    Args:
        since: ISO date (YYYY-MM-DD); stop paginating once orders predate it.
        until: ISO date (YYYY-MM-DD); skip orders on/after this date.
        max_pages: hard cap on pages fetched (safety).

    Rappi returns newest-first, so pagination stops early once a page's
    oldest order is before `since`.
    """
    results: list[dict] = []
    page = 1
    while page <= max_pages:
        payload = await get_order_history_page(client, page)
        rows = payload.get("data") or []
        if not rows:
            break
        for raw in rows:
            norm = _normalize_history_order(raw)
            created = (norm.get("created_at") or "")[:10]
            if until and created and created >= until:
                continue
            if since and created and created < since:
                continue
            results.append(norm)
        # Stop once the oldest order on this page predates `since`.
        oldest = (rows[-1].get("created_at") or "")[:10]
        if since and oldest and oldest < since:
            break
        last_page = payload.get("last_page") or page
        if page >= last_page:
            break
        page += 1
        # Be polite to the gateway — it 429s on rapid sequential paging.
        await asyncio.sleep(0.5)
    return results


async def get_order_resume(client: RappiClient, order_id: int) -> dict:
    """Get full order summary — products, totals, store, address."""
    path = Endpoints.ORDER_RESUME.format(order_id=order_id)
    return await client.get(path)


async def get_order_realtime_state(client: RappiClient, order_id: int) -> dict:
    """Get real-time order state — flow_key, timeline, ETA, map positions."""
    path = Endpoints.ORDER_REALTIME_STATE.format(order_id=order_id)
    return await client.get(path)


async def get_order_products(client: RappiClient, order_id: int) -> list[dict]:
    """Get order products detail."""
    path = Endpoints.ORDER_PRODUCTS.format(order_id=order_id)
    data = await client.get(path)
    if isinstance(data, list):
        return data
    return data.get("products", data.get("data", []))


async def get_order_cost_breakdown(client: RappiClient, order_id: int) -> dict:
    """Get full cost breakdown — subtotal, fees, discounts, tip."""
    path = Endpoints.ORDER_COST_BREAKDOWN.format(order_id=order_id)
    return await client.get(path)
