"""
Food-region handling for the nutrition tools (self-hosted fork).

Upstream hard-codes the US food region in every custom-food payload and lets
garminconnect's default "Accept-Language: en-US" ride along on the catalogue
search. This fork reads the region from GARMIN_FOOD_REGION (default GB), uses it
in all five payload sites, and asks the food search for the matching language.
"""
import pytest
from mcp.server.fastmcp import FastMCP

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
    return nutrition.register_tools(FastMCP("Test Nutrition Region"))


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


def test_accept_language_follows_region():
    assert nutrition._accept_language("GB") == "en-GB,en;q=0.9"
    assert nutrition.FOOD_ACCEPT_LANGUAGE == "en-GB,en;q=0.9"


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
    assert "Food logged successfully" in result[0][0].text
    # one regionCode in the create payload, one in the log payload
    assert _region_codes(_put_payloads(mock_garmin_client)) == ["IE", "IE"]


def test_no_hard_coded_us_region_left_in_module():
    import inspect
    assert '"regionCode": "US"' not in inspect.getsource(nutrition)


# --- catalogue search asks for the configured language ------------------------

@pytest.mark.asyncio
async def test_search_foods_sends_configured_accept_language(app_with_nutrition, mock_garmin_client):
    mock_garmin_client.connectapi.return_value = {"results": [], "moreDataAvailable": False}
    await app_with_nutrition.call_tool("search_foods", {"query": "Greggs"})
    mock_garmin_client.connectapi.assert_called_once_with(
        "/nutrition-service/food/search",
        params={"searchExpression": "Greggs", "start": 0, "limit": 20},
        headers={"Accept-Language": "en-GB,en;q=0.9"},
    )


@pytest.mark.asyncio
async def test_search_foods_accept_language_follows_override(app_with_nutrition, mock_garmin_client, monkeypatch):
    monkeypatch.setattr(nutrition, "FOOD_ACCEPT_LANGUAGE", "en-IE,en;q=0.9")
    mock_garmin_client.connectapi.return_value = {"results": [], "moreDataAvailable": False}
    await app_with_nutrition.call_tool("search_foods", {"query": "Tayto"})
    assert mock_garmin_client.connectapi.call_args[1]["headers"] == {"Accept-Language": "en-IE,en;q=0.9"}
