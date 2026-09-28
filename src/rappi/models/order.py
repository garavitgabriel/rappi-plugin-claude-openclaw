"""Order and checkout models."""

from pydantic import BaseModel, model_validator

from rappi.utils.pricing import strip_html


class CheckoutDetailItem(BaseModel):
    type: str | None = None
    key: str | None = None
    value: str | None = None


class CheckoutHeader(BaseModel):
    title: str | None = None
    image: str | None = None


class CheckoutSummary(BaseModel):
    header: CheckoutHeader | None = None
    details: list[CheckoutDetailItem] = []


class CheckoutDetail(BaseModel):
    return_key: str | None = None
    summary: list[CheckoutSummary] = []

    @model_validator(mode="after")
    def _strip_html_from_return_key(self):
        if self.return_key:
            self.return_key = strip_html(self.return_key)
        return self


class OrderStore(BaseModel):
    id: int
    name: str | None = None
    logo: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    type: str | None = None
    store_type: str | None = None


class Order(BaseModel):
    id: int
    total: float = 0
    state: str | None = None
    place_at: str | None = None
    eta: str | int | None = None  # API sends minutes as an int on live orders
    store: OrderStore | None = None
    delivery_method: str | None = None
    tip: float = 0
    can_be_cancel: bool = False


class OrdersResponse(BaseModel):
    active_orders: list[Order] = []
    cancel_orders: list[Order] = []
