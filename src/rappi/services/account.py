"""Account services — favorites from API, credits, active orders."""

from rappi.client import RappiClient
from rappi.constants import Endpoints, HEADERS_FAVORITES


async def get_favorite_stores_api(client: RappiClient) -> list[dict]:
    """Get favorite stores from Rappi's API (richer than local memory)."""
    try:
        data = await client.post(
            Endpoints.FAVORITE_STORES_API,
            json={"favorite_stores_type": "global", "lat": client.config.lat, "lng": client.config.lng},
            headers=HEADERS_FAVORITES,
        )
        if isinstance(data, list):
            return data
        return data.get("stores", data.get("data", []))
    except Exception:
        # Endpoint may need additional params — fall back gracefully
        return []


async def get_rappi_credits(client: RappiClient) -> dict:
    """Get Rappi credits/wallet balance."""
    data = await client.get(Endpoints.RAPPI_CREDITS)
    return data


async def get_active_orders_v3(client: RappiClient) -> list[dict]:
    """Get active orders using the newer v3 endpoint.

    v3 returns ``{"cards": [...], "show_widget": bool}``; each card carries
    ``order_id``/``state`` plus display ``texts`` (store name, status line).
    """
    data = await client.get(Endpoints.ACTIVE_ORDERS_V3)
    if isinstance(data, list):
        return data
    if "cards" in data:
        return [_normalize_order_card(c) for c in data["cards"] if isinstance(c, dict)]
    return data.get("orders", data.get("data", []))


def _normalize_order_card(card: dict) -> dict:
    """Flatten a v3 order card: texts[0] is the store name, texts[1] the status line."""
    texts = [t.get("text") for t in card.get("texts", []) if isinstance(t, dict)]
    return {
        **card,
        "store_name": texts[0] if texts else None,
        "status_text": " ".join(texts[1].split()) if len(texts) > 1 and texts[1] else None,
    }
