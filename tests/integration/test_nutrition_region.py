"""
Food-region handling for the nutrition tools (self-hosted fork).

Upstream hard-codes the US food region in every custom-food payload and sends the
catalogue search no region at all, so searches come back from the US catalogue.
This fork reads the region from GARMIN_FOOD_REGION (default GB), uses it in all
five payload sites, and sends regionCode/languageCode on the food search — the
parameters Garmin Connect web sends (captured 2026-09-20):

    /nutrition-service/food/search?searchExpression=gregg&start=0&limit=50&regionCode=GB&languageCode=en
"""
import json

import pytest
from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError
from mcp.server.mcpserver import MCPServer

from garmin_mcp import nutrition

MOCK_MEALS = {
    "meals": [
        {"mealId": 20250, "mealName": "LUNCH", "startTime": "11:00:00", "endTime": "14:00:00"},
        {"mealId": 20252, "mealName": "SNACKS"},
    ]
}


@pytest.fixture
def app_with_nutrition(mock_garmin_client):
    nutrition.configure(mock_garmin_client)
    return nutrition.register_tools(MCPServer("Test Nutrition Region"))


def _region_codes(obj) -> list:
    """Every regionCode value anywhere inside a request payload."""
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == "regionCode"] + \
               [c for k, v in obj.items() if k != "regionCode" for c in _region_codes(v)]
    if isinstance(obj, list):
        return [c for v in obj for c in _region_codes(v)]
    return []


def _put_payloads(mock_garmin_client) -> list:
    return [c[1]["json"] for c in mock_garmin_client.client.put.call_args_list]


# --- configuration -----------------------------------------------------------

def test_food_region_defaults_to_gb():
    assert nutrition._food_region({}) == "GB"
    assert nutrition.FOOD_REGION == "GB"          # nothing set in the test environment


def test_food_region_reads_env_and_normalises():
    assert nutrition._food_region({"GARMIN_FOOD_REGION": "ie"}) == "IE"
    assert nutrition._food_region({"GARMIN_FOOD_REGION": " us "}) == "US"


def test_food_region_blank_falls_back_to_default():
    assert nutrition._food_region({"GARMIN_FOOD_REGION": "  "}) == "GB"


# --- the five payload sites follow the constant, not a literal -----------------

@pytest.mark.asyncio
async def test_create_custom_food_uses_configured_region(app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_REGION", "IE")
    mock_garmin_client.client.put.return_value = {"foodId": "f1", "servingId": "s1"}
    await app_with_nutrition.call_tool(
        "create_custom_food", {"food_name": "Porridge", "calories": 150})
    assert _region_codes(_put_payloads(mock_garmin_client)) == ["IE"]


@pytest.mark.asyncio
async def test_create_custom_food_defaults_to_gb(app_with_nutrition, mock_garmin_client):
    mock_garmin_client.client.put.return_value = {"foodId": "f1", "servingId": "s1"}
    await app_with_nutrition.call_tool(
        "create_custom_food", {"food_name": "Porridge", "calories": 150})
    assert _region_codes(_put_payloads(mock_garmin_client)) == ["GB"]


@pytest.mark.asyncio
async def test_update_custom_food_uses_configured_region(app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_REGION", "IE")
    mock_garmin_client.connectapi.return_value = {"customFoods": [{
        "foodMetaData": {"foodId": "f1", "foodName": "Porridge", "regionCode": "US"},
        "nutritionContents": [{"servingId": "s1", "servingUnit": "G", "numberOfUnits": 100, "calories": 150}],
    }]}
    mock_garmin_client.client.put.return_value = {"foodId": "f1"}
    await app_with_nutrition.call_tool(
        "update_custom_food",
        {"food_id": "f1", "serving_id": "s1", "food_name": "Porridge", "calories": 160})
    codes = _region_codes(_put_payloads(mock_garmin_client))
    assert codes and set(codes) == {"IE"}


@pytest.mark.asyncio
async def test_log_custom_food_uses_configured_region(app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_REGION", "IE")
    mock_garmin_client.connectapi.return_value = MOCK_MEALS
    mock_garmin_client.client.put.return_value = {"status": "ok"}
    await app_with_nutrition.call_tool(
        "log_custom_food",
        {"meal_date": "2024-01-15", "meal_time": "12:30:00", "food_id": "f1",
         "serving_id": "s1", "serving_qty": 1})
    assert _region_codes(_put_payloads(mock_garmin_client)) == ["IE"]


@pytest.mark.asyncio
async def test_upsert_and_log_uses_configured_region_for_create_and_log(
        app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_REGION", "IE")
    mock_garmin_client.connectapi.side_effect = [{"customFoods": []}, MOCK_MEALS]
    mock_garmin_client.client.put.side_effect = [
        {"foodMetaData": {"foodId": "f9", "foodName": "New Food"},
         "nutritionContents": [{"servingId": "s9"}]},
        {},
    ]
    result = await app_with_nutrition.call_tool(
        "upsert_and_log",
        {"meal_date": "2024-01-15", "meal_time": "12:00:00", "food_name": "New Food",
         "calories": 200})
    assert "Food logged successfully" in result.content[0].text
    # one regionCode in the create payload, one in the log payload
    assert _region_codes(_put_payloads(mock_garmin_client)) == ["IE", "IE"]


def test_no_hard_coded_us_region_left_in_module():
    import inspect
    assert '"regionCode": "US"' not in inspect.getsource(nutrition)


# --- catalogue search sends the region, as Garmin Connect web does --------------

_EMPTY = {"results": [], "moreDataAvailable": False}
_ONE = {"results": [{"foodMetaData": {"foodId": "1", "foodName": "Sausage Roll", "foodType": "BRAND",
                                      "source": "FATSECRET", "brandName": "Greggs",
                                      "regionCode": "GB", "languageCode": "en"},
                     "nutritionContents": []}],
        "moreDataAvailable": False}


@pytest.mark.asyncio
async def test_search_foods_sends_region_and_language(app_with_nutrition, mock_garmin_client):
    mock_garmin_client.connectapi.return_value = _ONE
    result = await app_with_nutrition.call_tool("search_foods", {"query": "Greggs"})
    mock_garmin_client.connectapi.assert_called_once_with(
        "/nutrition-service/food/search",
        params={"searchExpression": "Greggs", "start": 0, "limit": 20,
                "regionCode": "GB", "languageCode": "en"},
    )
    data = json.loads(result.content[0].text)
    assert data["catalogue_region"] == "GB"
    assert data["results"][0]["region"] == "GB"


@pytest.mark.asyncio
async def test_search_foods_region_follows_configuration(app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_REGION", "IE")
    mock_garmin_client.connectapi.return_value = _EMPTY
    await app_with_nutrition.call_tool("search_foods", {"query": "Tayto"})
    assert mock_garmin_client.connectapi.call_args[1]["params"]["regionCode"] == "IE"


@pytest.mark.asyncio
async def test_search_foods_falls_back_when_region_is_rejected(app_with_nutrition, mock_garmin_client):
    """A 400 on the region-qualified search must not break search: retry without it."""
    mock_garmin_client.connectapi.side_effect = [
        GarminConnectConnectionError("API Error 400 - regionCode not allowed"), _ONE]
    result = await app_with_nutrition.call_tool("search_foods", {"query": "Greggs"})
    assert mock_garmin_client.connectapi.call_count == 2
    assert mock_garmin_client.connectapi.call_args_list[1][1]["params"] == {
        "searchExpression": "Greggs", "start": 0, "limit": 20}
    data = json.loads(result.content[0].text)
    assert data["count"] == 1
    assert data["catalogue_region"] is None       # tells the caller the region was not applied


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    GarminConnectTooManyRequestsError("API Error 429 - slow down"),
    GarminConnectConnectionError("API Error 500 - boom"),
])
async def test_search_foods_does_not_retry_on_other_errors(app_with_nutrition, mock_garmin_client, error):
    """Only a 400 means 'parameters rejected'. Never double up requests on a rate limit."""
    mock_garmin_client.connectapi.side_effect = error
    result = await app_with_nutrition.call_tool("search_foods", {"query": "Greggs"})
    assert mock_garmin_client.connectapi.call_count == 1
    assert "Error" in result.content[0].text


@pytest.mark.asyncio
async def test_search_foods_fallback_works_through_the_real_client_proxy():
    """In the running worker the client is wrapped by _GarminProxy, which re-raises
    Garmin errors with a hint added. The 400 fallback must survive that wrapping."""
    from garmin_mcp import _GarminProxy

    class FakeGarmin:
        def __init__(self):
            self.calls = []

        def connectapi(self, path, **kwargs):
            self.calls.append(kwargs["params"])
            if "regionCode" in kwargs["params"]:
                raise GarminConnectConnectionError("API Error 400 - regionCode not allowed")
            return _ONE

    fake = FakeGarmin()
    nutrition.configure(_GarminProxy(fake))
    app = nutrition.register_tools(MCPServer("Test Nutrition Proxy"))
    result = await app.call_tool("search_foods", {"query": "Greggs"})
    assert [("regionCode" in c) for c in fake.calls] == [True, False]
    data = json.loads(result.content[0].text)
    assert data["count"] == 1 and data["catalogue_region"] is None


def test_no_accept_language_override_left_in_module():
    """The Accept-Language experiment was disproven against the live API; keep it out."""
    import inspect
    assert "Accept-Language" not in inspect.getsource(nutrition)
