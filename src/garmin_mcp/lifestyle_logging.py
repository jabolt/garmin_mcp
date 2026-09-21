"""
Lifestyle Logging write tools for Garmin Connect MCP Server (self-hosted fork)

Garmin exposes Lifestyle Logging only in the phone app, and garminconnect can only
read it. The requests below follow what the Garmin Connect Android app (5.29) sends:
it PUTs a copy of the day's item with logStatus / updateTimestamp / details, sends
the FULL tracked-id list when tracking changes, and creates custom behaviours with a
temporary negative id. Spelling is Garmin's: "behaviour".

Every write reads first, matches a behaviour by id or exact name only, and reads back
afterwards, because this writes to a health log.
"""
import datetime
import json
from typing import Optional

# The garmin_client will be set by the main file
garmin_client = None

_BASE = "/lifestylelogging-service"
# The fields of the app's log DTO. The daily log returns more (e.g. sleepRelated); the
# app does not send those back, so neither do we.
_LOG_FIELDS = ("behaviourId", "measurementType", "calendarDate", "category", "name")
_TEMP_ID = -1000


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _check_date(date: str) -> Optional[str]:
    try:
        datetime.date.fromisoformat(date)
    except (TypeError, ValueError):
        return f"Invalid date {date!r}: use YYYY-MM-DD."
    return None


def _daily_items(date: str) -> list:
    data = garmin_client.connectapi(f"{_BASE}/dailyLog/{date}")
    return (data or {}).get("dailyLogsReport") or []


def _all_behaviours(date: str) -> list:
    return garmin_client.connectapi(f"{_BASE}/behaviours/{date}") or []


def _find(ref, items: list):
    """Match by numeric id or by exact name (case-insensitive). Never partial: a near
    miss on a health log is worse than asking again."""
    ref = str(ref).strip()
    if ref.lstrip("-").isdigit():
        hits = [i for i in items if i.get("behaviourId") == int(ref)]
    else:
        hits = [i for i in items if str(i.get("name", "")).strip().lower() == ref.lower()]
    if len(hits) > 1:
        raise ValueError(f"More than one behaviour is named {ref!r}; use its id: "
                         + ", ".join(str(i.get("behaviourId")) for i in hits))
    return hits[0] if hits else None


def _names(items: list) -> str:
    return ", ".join(sorted(str(i.get("name")) for i in items)) or "none"


def _summary(item: Optional[dict]) -> dict:
    if not item:
        return {}
    out = {"log_status": item.get("logStatus"), "updated": item.get("updateTimestamp")}
    if item.get("details"):
        out["amounts"] = {d.get("subTypeName"): d.get("amount") for d in item["details"]}
    return {k: v for k, v in out.items() if v is not None}


def _merge_amounts(existing: list, subtypes: list, amounts: dict, mode: str) -> list:
    """Apply amounts (keyed by subtype name or id) to the day's details, as the app does:
    insert or update by subTypeId. A subtype that ends at zero is removed."""
    by_id = {d.get("subTypeId"): dict(d) for d in existing or []}
    for key, amount in amounts.items():
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError(f"Amount for {key!r} must be a whole number.")
        ref = str(key).strip()
        match = [s for s in subtypes
                 if (ref.isdigit() and s.get("subTypeId") == int(ref)) or str(s.get("name", "")).lower() == ref.lower()]
        if not match:
            raise ValueError(f"Unknown subtype {key!r}. Valid: " + ", ".join(str(s.get("name")) for s in subtypes))
        sub_id, sub_name = match[0].get("subTypeId"), match[0].get("name")
        current = by_id.get(sub_id, {}).get("amount") or 0
        value = max(0, current + amount if mode == "add" else amount)
        if value:
            by_id[sub_id] = {"subTypeId": sub_id, "subTypeName": sub_name, "amount": value}
        else:
            by_id.pop(sub_id, None)
    return [by_id[k] for k in sorted(by_id)]


def _profile_pk(behaviours: list):
    for b in behaviours:
        if b.get("userProfilePk"):
            return b["userProfilePk"]
    try:        # same source the gear tools use
        return (garmin_client.get_device_last_used() or {}).get("userProfileNumber")
    except Exception:
        return None


def _put_tracked(behaviours: list, tracked_ids: set) -> None:
    payload = {"userProfilePk": _profile_pk(behaviours), "calendarDate": _now().date().isoformat(),
               "trackedBehaviours": sorted(tracked_ids)}
    if payload["userProfilePk"] is None:        # the app omits nulls
        del payload["userProfilePk"]
    garmin_client.client.put("connectapi", f"{_BASE}/trackedBehaviours", json=payload, api=True)


def register_tools(app):
    """Register all lifestyle logging write tools with the MCP server app"""

    @app.tool()
    async def get_lifestyle_behaviours(date: str) -> str:
        """List every Garmin Lifestyle Logging behaviour and whether it is tracked

        Returns Garmin's built-in behaviours (alcohol, caffeine, meals, exercise, sleep
        aids, treatments, life status...) and the user's custom ones, each with its id,
        category, measurement type (NONE = yes/no, QUANTITY = takes amounts), its subtypes
        (e.g. COFFEE, TEA for caffeine) and whether the user currently tracks it.

        Only tracked behaviours can be logged. Use this to find a behaviour's exact name,
        id and subtypes before log_lifestyle_behaviour or track_lifestyle_behaviours.
        For what was logged on a day, use get_lifestyle_logging_data.

        Args:
            date: Date in YYYY-MM-DD format
        """
        problem = _check_date(date)
        if problem:
            return problem
        try:
            behaviours = _all_behaviours(date)
            if not behaviours:
                return f"No lifestyle behaviours found for {date}."
            rows = []
            for b in behaviours:
                row = {"behaviour_id": b.get("behaviourId"), "name": b.get("name"), "category": b.get("category"),
                       "measurement_type": b.get("measurementType"), "tracked": bool(b.get("tracked")),
                       "sleep_related": bool(b.get("sleepRelated"))}
                if b.get("subTypes"):
                    row["subtypes"] = [{"subtype_id": s.get("subTypeId"), "name": s.get("name")} for s in b["subTypes"]]
                rows.append(row)
            return json.dumps({"count": len(rows), "tracked_count": sum(r["tracked"] for r in rows),
                               "behaviours": rows}, indent=2)
        except Exception as e:
            return f"Error retrieving lifestyle behaviours: {str(e)}"

    @app.tool()
    async def log_lifestyle_behaviour(
        date: str,
        behaviour: str,
        status: str = "YES",
        amounts: Optional[dict] = None,
        mode: str = "set",
    ) -> str:
        """Log a Garmin Lifestyle Logging behaviour for a day (today or any past date)

        Marks a tracked behaviour as done (YES) or not done (NO), with amounts for
        quantity behaviours. Also use this to correct an earlier entry: log the same
        date again with the right values. Returns the entry before and after.

        When you log a caffeinated drink (coffee, tea, cola, energy drink) or an alcoholic
        drink with the food tools, also record it here, since food logging does not carry
        caffeine or alcohol into Lifestyle Logging. Garmin splits caffeine into two
        behaviours, "Morning Caffeine" and "Late Caffeine"; if it is unclear which one a
        drink belongs to, ask the user rather than choosing.

        The behaviour must already be tracked (see track_lifestyle_behaviours). Match is by
        id or exact name only; call get_lifestyle_behaviours if unsure.

        Args:
            date: Date in YYYY-MM-DD format
            behaviour: Behaviour id or exact name (e.g. "Morning Caffeine", "Alcohol", "84631")
            status: "YES" (done) or "NO" (not done). NO removes any amounts.
            amounts: For quantity behaviours, amount per subtype name or id, e.g.
                {"COFFEE": 2} or {"WINE": 1, "BEER": 2}. Whole numbers.
            mode: "set" (default) makes each given subtype exactly that amount and is safe
                to repeat; "add" adds to what is already logged (use for "one more coffee").
        """
        problem = _check_date(date)
        if problem:
            return problem
        status = str(status).strip().upper()
        mode = str(mode).strip().lower()
        if status not in ("YES", "NO"):
            return "status must be YES or NO."
        if mode not in ("set", "add"):
            return 'mode must be "set" or "add".'
        if amounts and status == "NO":
            return "status NO cannot be combined with amounts: NO clears them."
        try:
            items = _daily_items(date)
            item = _find(behaviour, items)
            if item is None:
                known = _find(behaviour, _all_behaviours(date))
                if known is not None:
                    return (f"{known.get('name')} is not tracked, so it cannot be logged. "
                            f"Add it with track_lifestyle_behaviours first.")
                return f"Behaviour {behaviour!r} not found. Tracked on {date}: {_names(items)}."

            payload = {k: item[k] for k in _LOG_FIELDS if item.get(k) is not None}
            details, note = item.get("details"), None
            if amounts:
                if item.get("measurementType") != "QUANTITY":
                    return f"{item.get('name')} is a yes/no behaviour and takes no amounts."
                catalogue = _find(item.get("behaviourId"), _all_behaviours(date)) or {}
                subtypes = catalogue.get("subTypes") or []
                if not subtypes:
                    return (f"{item.get('name')} has no subtypes; amounts for such behaviours are not "
                            f"supported yet (Garmin's request shape for them is unconfirmed).")
                details = _merge_amounts(details, subtypes, amounts, mode)
                if not details:
                    status, note = "NO", "No amounts left, so it was logged as NO (as the Garmin app does)."
            payload["logStatus"] = status
            payload["updateTimestamp"] = _now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            if status == "YES" and details:
                payload["details"] = details

            garmin_client.client.put(
                "connectapi", f"{_BASE}/dailyLog/{item['behaviourId']}/{date}", json=payload, api=True
            )
            after = _find(item["behaviourId"], _daily_items(date))
            result = {"status": "logged", "date": date, "behaviour": item.get("name"),
                      "behaviour_id": item.get("behaviourId"), "before": _summary(item), "after": _summary(after)}
            if note:
                result["note"] = note
            return json.dumps(result, indent=2)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error logging lifestyle behaviour: {str(e)}"

    @app.tool()
    async def clear_lifestyle_behaviour_log(date: str, behaviour: str) -> str:
        """Remove a day's Lifestyle Logging entry for one behaviour (back to unanswered)

        Deletes what was logged for that behaviour on that date, leaving it neither YES
        nor NO. The behaviour stays tracked. To change an entry rather than remove it, use
        log_lifestyle_behaviour with the corrected values.

        Args:
            date: Date in YYYY-MM-DD format
            behaviour: Behaviour id or exact name
        """
        problem = _check_date(date)
        if problem:
            return problem
        try:
            items = _daily_items(date)
            item = _find(behaviour, items)
            if item is None:
                return f"Behaviour {behaviour!r} not found. Tracked on {date}: {_names(items)}."
            if not item.get("logStatus"):
                return f"Nothing is logged for {item.get('name')} on {date}."
            garmin_client.client.delete("connectapi", f"{_BASE}/dailyLog/{item['behaviourId']}/{date}", api=True)
            after = _find(item["behaviourId"], _daily_items(date))
            return json.dumps({"status": "cleared", "date": date, "behaviour": item.get("name"),
                               "behaviour_id": item.get("behaviourId"), "before": _summary(item),
                               "after": _summary(after)}, indent=2)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error clearing lifestyle behaviour log: {str(e)}"

    @app.tool()
    async def track_lifestyle_behaviours(
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
    ) -> str:
        """Start or stop tracking Lifestyle Logging behaviours

        Adds behaviours to, or removes them from, the set the user tracks each day.
        Tracking a behaviour makes it appear in the daily log so it can be logged;
        untracking hides it without deleting what was logged before. Works for built-in
        and custom behaviours. Returns the tracked names before and after.

        Args:
            add: Behaviour ids or exact names to start tracking, e.g. ["Morning Caffeine", "Alcohol"]
            remove: Behaviour ids or exact names to stop tracking
        """
        add, remove = add or [], remove or []
        if not add and not remove:
            return "Supply add and/or remove."
        try:
            today = _now().date().isoformat()
            behaviours = _all_behaviours(today)
            resolved = {}
            for ref in list(add) + list(remove):
                match = _find(ref, behaviours)
                if match is None:
                    return (f"Behaviour {ref!r} not found. Use get_lifestyle_behaviours for the exact names, "
                            f"or create_custom_lifestyle_behaviour for a new one.")
                resolved[str(ref)] = match["behaviourId"]
            add_ids, remove_ids = {resolved[str(r)] for r in add}, {resolved[str(r)] for r in remove}
            if add_ids & remove_ids:
                return "The same behaviour is in both add and remove."
            before = {b["behaviourId"] for b in behaviours if b.get("tracked")}
            wanted = (before | add_ids) - remove_ids
            if wanted == before:
                return "No change: those behaviours are already in that state."
            _put_tracked(behaviours, wanted)
            after = {b["behaviourId"] for b in _all_behaviours(today) if b.get("tracked")}
            name = {b["behaviourId"]: b.get("name") for b in behaviours}
            return json.dumps({"status": "updated",
                               "tracked_before": sorted(str(name.get(i, i)) for i in before),
                               "tracked_after": sorted(str(name.get(i, i)) for i in after)}, indent=2)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error updating tracked lifestyle behaviours: {str(e)}"

    @app.tool()
    async def create_custom_lifestyle_behaviour(
        name: str,
        sleep_related: bool = False,
        track: bool = True,
    ) -> str:
        """Create a custom yes/no Lifestyle Logging behaviour (e.g. "Magnesium", "Nap")

        Check get_lifestyle_behaviours first: Garmin has about 45 built-in behaviours, and a
        built-in one should be tracked rather than recreated. Custom behaviours are yes/no
        only here (no amounts).

        Args:
            name: Name of the new behaviour
            sleep_related: True if it concerns the night's sleep (Garmin groups these separately)
            track: Start tracking it straight away (default True)
        """
        name = str(name or "").strip()
        if not name:
            return "A behaviour name is required."
        try:
            today = _now().date().isoformat()
            behaviours = _all_behaviours(today)
            if _find(name, behaviours) is not None:
                return f"A behaviour named {name!r} already exists; track it with track_lifestyle_behaviours."
            new = {"behaviourId": _TEMP_ID, "measurementType": "NONE", "name": name,
                   "category": "CUSTOM_SLEEP_RELATED" if sleep_related else "CUSTOM",
                   "tracked": bool(track), "sleepRelated": bool(sleep_related)}
            created = garmin_client.client.post("connectapi", f"{_BASE}/behaviours/batch", json=[new], api=True)
            made = (created or [{}])[0] if isinstance(created, list) else {}
            new_id = made.get("behaviourId")
            if new_id is None or new_id < 0:
                return "Garmin accepted the request but did not return the new behaviour's id; check the app."
            if track:
                tracked = {b["behaviourId"] for b in behaviours if b.get("tracked")} | {new_id}
                _put_tracked(behaviours + [made], tracked)
            return json.dumps({"status": "created", "behaviour_id": new_id, "name": made.get("name", name),
                               "category": made.get("category", new["category"]), "tracked": bool(track)}, indent=2)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error creating lifestyle behaviour: {str(e)}"

    @app.tool()
    async def delete_custom_lifestyle_behaviour(behaviour: str) -> str:
        """Delete a custom Lifestyle Logging behaviour. Confirm with the user first.

        Only behaviours the user created can be deleted, and their logged history may go
        with them. To simply stop seeing a behaviour, untrack it with
        track_lifestyle_behaviours instead: that keeps its history.

        Args:
            behaviour: Custom behaviour id or exact name
        """
        try:
            behaviours = _all_behaviours(_now().date().isoformat())
            match = _find(behaviour, behaviours)
            if match is None:
                return f"Behaviour {behaviour!r} not found."
            if not str(match.get("category", "")).startswith("CUSTOM"):
                return (f"{match.get('name')} is a built-in behaviour and cannot be deleted. "
                        f"Untrack it with track_lifestyle_behaviours instead.")
            garmin_client.client.delete("connectapi", f"{_BASE}/behaviours/{match['behaviourId']}", api=True)
            return json.dumps({"status": "deleted", "behaviour_id": match["behaviourId"],
                               "name": match.get("name")}, indent=2)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error deleting lifestyle behaviour: {str(e)}"

    return app
