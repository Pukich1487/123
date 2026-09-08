import json

import httpx
import pytest

from kosell_bot.kosell import KosellClient


@pytest.mark.asyncio
async def test_rent_sends_idempotency_in_header_and_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "rental_uid": "uid-1",
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )

    client = KosellClient("secret", "https://example.test/api/v1")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://example.test/api/v1",
        headers={"X-API-Key": "secret"},
        transport=httpx.MockTransport(handler),
    )

    result = await client.rent(7, 24, "RUB", "playerok-deal-1")

    assert result["rental_uid"] == "uid-1"
    assert captured["header"] == "playerok-deal-1"
    assert captured["body"]["idempotency_key"] == "playerok-deal-1"
    assert captured["body"]["duration_hours"] == 24
    await client.close()

