"""
Integration tests for the lifestyle_logging module (self-hosted fork).

Write support for Garmin Lifestyle Logging. The request shapes follow the contract
recovered from the Garmin Connect Android app (5.29): the app PUTs a copy of the
day's item with logStatus / updateTimestamp / details, sends the FULL tracked-id
list when tracking changes, and creates custom behaviours with a temporary
negative id. Garmin is faked here by a small stateful stand-in so each write can be
checked together with its read-back.
"""
import copy
import datetime
import json
import re

import pytest
from garminconnect import GarminConnectConnectionError
from mcp.server.mcpserver import MCPServer

from garmin_mcp import lifestyle_logging, nutrition

DAY = "2026-09-21"
NOW = datetime.datetime(2026, 9, 21, 8, 30, 15, 123456)
LOG_FIELDS = {"behaviourId", "measurementType", "calendarDate", "category",
              "logStatus", "name", "updateTimestamp", "details"}

CAFFEINE_SUBTYPES = [{"subTypeId": 1, "name": "COFFEE"}, {"subTypeId": 2, "name": "TEA"},
                     {"subTypeId": 3, "name": "OTHER"}, {"subTypeId": 4, "name": "SODA"},
                     {"subTypeId": 5, "name": "ENERGY_DRINK"}]


def _behaviours():
    return [
        {"behaviourId": 84631, "userProfilePk": 7654321, "measurementType": "NONE", "name": "Medication",
         "category": "CUSTOM", "tracked": True, "sleepRelated": False, "hidden": False},
        {"behaviourId": 2, "measurementType": "QUANTITY", "name": "Morning Caffeine",
         "category": "LIFESTYLE", "tracked": True, "sleepRelated": False, "subTypes": CAFFEINE_SUBTYPES},
        {"behaviourId": 1, "measurementType": "QUANTITY", "name": "Alcohol", "category": "LIFESTYLE",
         "tracked": False, "sleepRelated": False,
         "subTypes": [{"subTypeId": 1, "name": "BEER"}, {"subTypeId": 2, "name": "WINE"}]},
        {"behaviourId": 12, "measurementType": "NONE", "name": "Sunlight", "category": "SELF_CARE",
         "tracked": False, "sleepRelated": False},
    ]


def _daily():
    return {
        "calendarDate": DAY,
        "dailyLogsReport": [
            {"behaviourId": 84631, "measurementType": "NONE", "calendarDate": DAY, "name": "Medication",
             "category": "CUSTOM", "sleepRelated": False},
            {"behaviourId": 2, "measurementType": "QUANTITY", "calendarDate": DAY, "name": "Morning Caffeine",
             "category": "LIFESTYLE", "sleepRelated": False, "logStatus": "YES",
             "updateTimestamp": "2026-09-21T07:00:00.000",
             "details": [{"subTypeId": 1, "subTypeName": "COFFEE", "amount": 1},
                         {"subTypeId": 2, "subTypeName": "TEA", "amount": 3}]},
        ],
        "completionStats": [{"calendarDate": DAY, "totalTracking": 2, "completedTracking": 1}],
    }


class FakeLifestyleService:
    """Stateful stand-in for /lifestylelogging-service, attached to the mock client."""

    def __init__(self, client):
        self.daily, self.behaviours = _daily(), _behaviours()
        self.calls = []                       # (verb, url, json)
        self.next_id = 90001
        client.connectapi.side_effect = self.get
        client.client.put.side_effect = lambda _d, url, **kw: self.write("PUT", url, kw.get("json"))
        client.client.post.side_effect = lambda _d, url, **kw: self.write("POST", url, kw.get("json"))
        client.client.delete.side_effect = lambda _d, url, **kw: self.write("DELETE", url, kw.get("json"))

    def get(self, url, **_kw):
        self.calls.append(("GET", url, None))
        if "/dailyLog/" in url:
            return copy.deepcopy(self.daily)
        if "/behaviours/" in url:
            return copy.deepcopy(self.behaviours)
        raise AssertionError(f"unexpected GET {url}")

    def write(self, verb, url, body):
        self.calls.append((verb, url, copy.deepcopy(body)))
        m = re.fullmatch(r"/lifestylelogging-service/dailyLog/(-?\d+)/(\d{4}-\d\d-\d\d)", url)
        if m:
            item = next(i for i in self.daily["dailyLogsReport"] if i["behaviourId"] == int(m.group(1)))
            for key in ("logStatus", "updateTimestamp", "details"):
                item.pop(key, None)
            if verb == "PUT":
                item.update({k: v for k, v in body.items() if k in ("logStatus", "updateTimestamp", "details")})
            return {}
        if url == "/lifestylelogging-service/trackedBehaviours":
            for b in self.behaviours:
                b["tracked"] = b["behaviourId"] in body["trackedBehaviours"]
            return body
        if url == "/lifestylelogging-service/behaviours/batch":
            created = []
            for b in body:
                new = dict(b, behaviourId=self.next_id, userProfilePk=7654321)
                self.next_id += 1
                self.behaviours.append(new)
                created.append(copy.deepcopy(new))
            return created
        m = re.fullmatch(r"/lifestylelogging-service/behaviours/(-?\d+)", url)
        if m and verb == "DELETE":
            self.behaviours = [b for b in self.behaviours if b["behaviourId"] != int(m.group(1))]
            return {}
        raise AssertionError(f"unexpected {verb} {url}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def service(mock_garmin_client, monkeypatch):
    monkeypatch.setattr(lifestyle_logging, "_now", lambda: NOW)
    lifestyle_logging.configure(mock_garmin_client)
    return FakeLifestyleService(mock_garmin_client)


@pytest.fixture
def app(service):
    return lifestyle_logging.register_tools(MCPServer("Test Lifestyle"))


async def _call(app, tool, args):
    result = await app.call_tool(tool, args)
    return result.content[0].text


# --- registration -------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_six_tools_are_registered(app):
    names = {t.name for t in await app.list_tools()}
    assert names == {"get_lifestyle_behaviours", "log_lifestyle_behaviour", "clear_lifestyle_behaviour_log",
                     "track_lifestyle_behaviours", "create_custom_lifestyle_behaviour",
                     "delete_custom_lifestyle_behaviour"}


# --- get_lifestyle_behaviours ---------------------------------------------------

@pytest.mark.asyncio
async def test_get_behaviours_lists_tracked_flags_and_subtypes(app, service):
    data = json.loads(await _call(app, "get_lifestyle_behaviours", {"date": DAY}))
    assert service.calls == [("GET", f"/lifestylelogging-service/behaviours/{DAY}", None)]
    assert data["count"] == 4 and data["tracked_count"] == 2
    caffeine = next(b for b in data["behaviours"] if b["name"] == "Morning Caffeine")
    assert caffeine == {"behaviour_id": 2, "name": "Morning Caffeine", "category": "LIFESTYLE",
                        "measurement_type": "QUANTITY", "tracked": True, "sleep_related": False,
                        "subtypes": [{"subtype_id": s["subTypeId"], "name": s["name"]} for s in CAFFEINE_SUBTYPES]}


@pytest.mark.asyncio
async def test_get_behaviours_empty(app, service):
    service.behaviours = []
    assert "No lifestyle behaviours found" in await _call(app, "get_lifestyle_behaviours", {"date": DAY})


# --- log_lifestyle_behaviour ----------------------------------------------------

@pytest.mark.asyncio
async def test_log_yes_sends_the_apps_payload_and_reads_back(app, service):
    data = json.loads(await _call(app, "log_lifestyle_behaviour",
                                  {"date": DAY, "behaviour": "Medication", "status": "YES"}))
    (verb, url, body), = service.writes()
    assert (verb, url) == ("PUT", f"/lifestylelogging-service/dailyLog/84631/{DAY}")
    assert body == {"behaviourId": 84631, "measurementType": "NONE", "calendarDate": DAY,
                    "category": "CUSTOM", "logStatus": "YES", "name": "Medication",
                    "updateTimestamp": "2026-09-21T08:30:15.123"}
    assert set(body) <= LOG_FIELDS and "sleepRelated" not in body     # the app's DTO, not a blind echo
    assert data["status"] == "logged" and data["behaviour"] == "Medication"
    assert data["before"].get("log_status") is None and data["after"]["log_status"] == "YES"


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["84631", "medication", "  MEDICATION "])
async def test_behaviour_is_matched_by_id_or_exact_name(app, service, ref):
    await _call(app, "log_lifestyle_behaviour", {"date": DAY, "behaviour": ref, "status": "YES"})
    assert service.writes()[0][1].endswith(f"/84631/{DAY}")


@pytest.mark.asyncio
async def test_partial_names_never_match(app, service):
    text = await _call(app, "log_lifestyle_behaviour", {"date": DAY, "behaviour": "Medic", "status": "YES"})
    assert "not found" in text and "Medication" in text and service.writes() == []


@pytest.mark.asyncio
async def test_log_no_drops_the_amounts(app, service):
    await _call(app, "log_lifestyle_behaviour", {"date": DAY, "behaviour": "Morning Caffeine", "status": "NO"})
    body = service.writes()[0][2]
    assert body["logStatus"] == "NO" and "details" not in body


@pytest.mark.asyncio
async def test_amounts_set_replaces_only_the_named_subtype(app, service):
    data = json.loads(await _call(app, "log_lifestyle_behaviour",
                                  {"date": DAY, "behaviour": "Morning Caffeine", "amounts": {"coffee": 2}}))
    body = service.writes()[0][2]
    assert body["logStatus"] == "YES"
    assert body["details"] == [{"subTypeId": 1, "subTypeName": "COFFEE", "amount": 2},
                               {"subTypeId": 2, "subTypeName": "TEA", "amount": 3}]
    assert data["after"]["amounts"] == {"COFFEE": 2, "TEA": 3}


@pytest.mark.asyncio
async def test_amounts_add_increments_and_inserts(app, service):
    await _call(app, "log_lifestyle_behaviour",
                {"date": DAY, "behaviour": "2", "amounts": {"COFFEE": 1, "4": 2}, "mode": "add"})
    assert service.writes()[0][2]["details"] == [
        {"subTypeId": 1, "subTypeName": "COFFEE", "amount": 2},
        {"subTypeId": 2, "subTypeName": "TEA", "amount": 3},
        {"subTypeId": 4, "subTypeName": "SODA", "amount": 2}]


@pytest.mark.asyncio
async def test_add_never_goes_below_zero_and_zero_removes_the_subtype(app, service):
    await _call(app, "log_lifestyle_behaviour",
                {"date": DAY, "behaviour": "Morning Caffeine", "amounts": {"COFFEE": -5}, "mode": "add"})
    assert service.writes()[0][2]["details"] == [{"subTypeId": 2, "subTypeName": "TEA", "amount": 3}]


@pytest.mark.asyncio
async def test_removing_the_last_amount_logs_no_like_the_app(app, service):
    data = json.loads(await _call(app, "log_lifestyle_behaviour",
                                  {"date": DAY, "behaviour": "Morning Caffeine", "amounts": {"COFFEE": 0, "TEA": 0}}))
    body = service.writes()[0][2]
    assert body["logStatus"] == "NO" and "details" not in body
    assert "no amounts left" in data["note"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("args, expected", [
    ({"behaviour": "Morning Caffeine", "amounts": {"ESPRESSO": 1}}, "Unknown subtype"),
    ({"behaviour": "Medication", "amounts": {"COFFEE": 1}}, "yes/no behaviour"),
    ({"behaviour": "Morning Caffeine", "status": "NO", "amounts": {"COFFEE": 1}}, "cannot be combined"),
    ({"behaviour": "Medication", "status": "MAYBE"}, "status must be YES or NO"),
    ({"behaviour": "Medication", "mode": "multiply"}, "mode must be"),
    ({"behaviour": "Morning Caffeine", "amounts": {"COFFEE": 1.5}}, "whole number"),
])
async def test_invalid_input_is_rejected_before_any_write(app, service, args, expected):
    text = await _call(app, "log_lifestyle_behaviour", {"date": DAY, **args})
    assert expected in text and service.writes() == []


@pytest.mark.asyncio
async def test_untracked_behaviour_asks_to_track_first(app, service):
    text = await _call(app, "log_lifestyle_behaviour", {"date": DAY, "behaviour": "Alcohol", "status": "YES"})
    assert "not tracked" in text and "track_lifestyle_behaviours" in text and service.writes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["log_lifestyle_behaviour", "clear_lifestyle_behaviour_log"])
async def test_bad_date_touches_nothing(app, service, tool):
    text = await _call(app, tool, {"date": "21/09/2026", "behaviour": "Medication"})
    assert "YYYY-MM-DD" in text and service.calls == []


@pytest.mark.asyncio
async def test_garmin_errors_come_back_as_a_clean_message(app, service, mock_garmin_client):
    mock_garmin_client.client.put.side_effect = GarminConnectConnectionError("API Error 400 - bad field")
    text = await _call(app, "log_lifestyle_behaviour", {"date": DAY, "behaviour": "Medication"})
    assert text.startswith("Error logging lifestyle behaviour") and "400" in text


# --- clear_lifestyle_behaviour_log ----------------------------------------------

@pytest.mark.asyncio
async def test_clear_deletes_the_entry_and_reads_back(app, service):
    data = json.loads(await _call(app, "clear_lifestyle_behaviour_log", {"date": DAY, "behaviour": "Morning Caffeine"}))
    assert service.writes() == [("DELETE", f"/lifestylelogging-service/dailyLog/2/{DAY}", None)]
    assert data["status"] == "cleared" and data["before"]["log_status"] == "YES"
    assert data["after"].get("log_status") is None


@pytest.mark.asyncio
async def test_clear_with_nothing_logged_does_not_call_garmin(app, service):
    text = await _call(app, "clear_lifestyle_behaviour_log", {"date": DAY, "behaviour": "Medication"})
    assert "Nothing is logged" in text and service.writes() == []


# --- track_lifestyle_behaviours -------------------------------------------------

@pytest.mark.asyncio
async def test_track_sends_the_full_id_list_like_the_app(app, service):
    data = json.loads(await _call(app, "track_lifestyle_behaviours", {"add": ["Alcohol"]}))
    assert service.writes() == [("PUT", "/lifestylelogging-service/trackedBehaviours",
                                 {"userProfilePk": 7654321, "calendarDate": DAY, "trackedBehaviours": [1, 2, 84631]})]
    assert data["tracked_before"] == ["Medication", "Morning Caffeine"]
    assert data["tracked_after"] == ["Alcohol", "Medication", "Morning Caffeine"]


@pytest.mark.asyncio
async def test_untrack_and_track_together(app, service):
    await _call(app, "track_lifestyle_behaviours", {"add": ["12"], "remove": ["medication"]})
    assert service.writes()[0][2]["trackedBehaviours"] == [2, 12]


@pytest.mark.asyncio
@pytest.mark.parametrize("args, expected", [
    ({"add": ["Medication"]}, "No change"),
    ({"add": ["Yoga"]}, "not found"),
    ({}, "Supply add and/or remove"),
    ({"add": ["Alcohol"], "remove": ["Alcohol"]}, "both add and remove"),
])
async def test_track_rejects_or_skips_without_writing(app, service, args, expected):
    assert expected in await _call(app, "track_lifestyle_behaviours", args) and service.writes() == []


@pytest.mark.asyncio
async def test_profile_pk_falls_back_to_the_last_used_device_then_is_omitted(app, service, mock_garmin_client):
    for b in service.behaviours:
        b.pop("userProfilePk", None)
    mock_garmin_client.get_device_last_used.return_value = {"userProfileNumber": 1112223}
    await _call(app, "track_lifestyle_behaviours", {"add": ["Alcohol"]})
    assert service.writes()[-1][2]["userProfilePk"] == 1112223
    mock_garmin_client.get_device_last_used.return_value = None
    await _call(app, "track_lifestyle_behaviours", {"add": ["Sunlight"]})
    assert "userProfilePk" not in service.writes()[-1][2]        # the app omits nulls too


# --- custom behaviours ----------------------------------------------------------

@pytest.mark.asyncio
async def test_create_custom_posts_a_temporary_id_then_tracks_the_real_one(app, service):
    data = json.loads(await _call(app, "create_custom_lifestyle_behaviour", {"name": " Magnesium "}))
    post, put = service.writes()
    assert post == ("POST", "/lifestylelogging-service/behaviours/batch",
                    [{"behaviourId": -1000, "measurementType": "NONE", "name": "Magnesium",
                      "category": "CUSTOM", "tracked": True, "sleepRelated": False}])
    assert put[1] == "/lifestylelogging-service/trackedBehaviours"
    assert put[2]["trackedBehaviours"] == [2, 84631, 90001]
    assert data == {"status": "created", "behaviour_id": 90001, "name": "Magnesium",
                    "category": "CUSTOM", "tracked": True}


@pytest.mark.asyncio
async def test_create_sleep_related_untracked(app, service):
    await _call(app, "create_custom_lifestyle_behaviour", {"name": "Nap", "sleep_related": True, "track": False})
    (verb, _url, body), = service.writes()
    assert verb == "POST" and body[0]["category"] == "CUSTOM_SLEEP_RELATED"
    assert body[0]["sleepRelated"] is True and body[0]["tracked"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("name, expected", [("   ", "name is required"), ("medication", "already exists"),
                                            ("Sunlight", "already exists")])
async def test_create_refuses_blank_and_duplicate_names(app, service, name, expected):
    assert expected in await _call(app, "create_custom_lifestyle_behaviour", {"name": name})
    assert service.writes() == []


@pytest.mark.asyncio
async def test_delete_custom_behaviour(app, service):
    data = json.loads(await _call(app, "delete_custom_lifestyle_behaviour", {"behaviour": "Medication"}))
    assert service.writes() == [("DELETE", "/lifestylelogging-service/behaviours/84631", None)]
    assert data == {"status": "deleted", "behaviour_id": 84631, "name": "Medication"}


@pytest.mark.asyncio
async def test_delete_refuses_built_in_behaviours(app, service):
    text = await _call(app, "delete_custom_lifestyle_behaviour", {"behaviour": "Alcohol"})
    assert "built-in" in text and "track_lifestyle_behaviours" in text and service.writes() == []


# --- the link from food logging -------------------------------------------------

@pytest.mark.asyncio
async def test_food_logging_tools_point_at_lifestyle_logging(mock_garmin_client):
    nutrition.configure(mock_garmin_client)
    tools = {t.name: t.description for t in await nutrition.register_tools(MCPServer("n")).list_tools()}
    for name in ("upsert_and_log", "log_custom_food"):
        assert "log_lifestyle_behaviour" in tools[name], name


@pytest.mark.asyncio
async def test_log_tool_description_explains_the_drink_link_and_the_caffeine_split(app):
    tools = {t.name: t.description for t in await app.list_tools()}
    text = tools["log_lifestyle_behaviour"]
    assert "caffein" in text.lower() and "alcohol" in text.lower()
    assert "Morning Caffeine" in text and "Late Caffeine" in text and "ask" in text.lower()
