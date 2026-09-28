"""Tests for rappi.services.order — order listing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rappi.services.account import get_active_orders_v3
from rappi.services.order import get_orders
from rappi.models.order import OrdersResponse


class TestGetOrders:
    async def test_returns_active_and_cancelled(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{}'
        mock_response.json.return_value = {
            "active_orders": [
                {
                    "id": 1001,
                    "total": 45000,
                    "state": "in_store",
                    "store": {"id": 100, "name": "Burger Place"},
                    "tip": 3000,
                },
            ],
            "cancel_orders": [
                {
                    "id": 1002,
                    "total": 30000,
                    "state": "cancelled",
                    "store": {"id": 200, "name": "Pizza Place"},
                },
            ],
        }
        mock_client._http.request = AsyncMock(return_value=mock_response)

        result = await get_orders(mock_client)
        assert isinstance(result, OrdersResponse)
        assert len(result.active_orders) == 1
        assert result.active_orders[0].total == 45000
        assert result.active_orders[0].store.name == "Burger Place"
        assert len(result.cancel_orders) == 1
        assert result.cancel_orders[0].state == "cancelled"

    async def test_empty_orders(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{}'
        mock_response.json.return_value = {
            "active_orders": [],
            "cancel_orders": [],
        }
        mock_client._http.request = AsyncMock(return_value=mock_response)

        result = await get_orders(mock_client)
        assert result.active_orders == []
        assert result.cancel_orders == []

    async def test_int_eta_on_live_order(self, mock_client):
        # Live orders send eta as integer minutes, not a string
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{}'
        mock_response.json.return_value = {
            "active_orders": [
                {"id": 1003, "total": 52400.0, "state": "pending_review", "eta": 24, "tip": 2000.0},
            ],
            "cancel_orders": [],
        }
        mock_client._http.request = AsyncMock(return_value=mock_response)

        result = await get_orders(mock_client)
        assert result.active_orders[0].eta == 24


class TestGetActiveOrdersV3:
    async def test_parses_cards(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{}'
        mock_response.json.return_value = {
            "cards": [
                {
                    "order_id": 2001,
                    "state": "pending_review",
                    "texts": [
                        {"text": "Sushi Place", "type": "text"},
                        {"text": "Order \ndelivered", "type": "text"},
                        {"text": "Delivery time: 12:41 PM", "type": "text"},
                    ],
                },
            ],
            "show_widget": True,
        }
        mock_client._http.request = AsyncMock(return_value=mock_response)

        orders = await get_active_orders_v3(mock_client)
        assert len(orders) == 1
        assert orders[0]["order_id"] == 2001
        assert orders[0]["store_name"] == "Sushi Place"
        assert orders[0]["status_text"] == "Order delivered"

    async def test_empty_cards(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{}'
        mock_response.json.return_value = {"cards": [], "show_widget": False}
        mock_client._http.request = AsyncMock(return_value=mock_response)

        assert await get_active_orders_v3(mock_client) == []
