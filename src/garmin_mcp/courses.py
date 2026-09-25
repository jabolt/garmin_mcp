"""
Course management functions for Garmin Connect MCP Server.

Adds support for uploading GPX files as Garmin Connect Courses, listing courses,
getting detailed course waypoints/metadata, downloading course GPX files, and deleting courses.
"""

import io
import json
import math
import os
import pathlib
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional
from urllib.parse import quote

# The garmin_client will be set by the main file
garmin_client = None


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


_EARTH_RADIUS_M = 6371000.0


def _haversine(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    lat1, lon1 = math.radians(p1["latitude"]), math.radians(p1["longitude"])
    lat2, lon2 = math.radians(p2["latitude"]), math.radians(p2["longitude"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _initial_bearing(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    lat1, lat2 = math.radians(p1["latitude"]), math.radians(p2["latitude"])
    dlon = math.radians(p2["longitude"] - p1["longitude"])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    return (initial_bearing + 360) % 360


_ACTIVITY_TYPE_IDS = {
    "running": 1,
    "cycling": 2,
    "hiking": 3,
    "walking": 4,
    "trail_running": 5,
    "mountain_biking": 6,
    "road_biking": 7,
    "gravel_cycling": 8,
}


def _build_course_payload(
    parsed_skeleton: Dict[str, Any],
    course_name: str,
    activity_type_id: int,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    geo_points = parsed_skeleton.get("geoPoints", [])
    if not geo_points:
        raise ValueError("GPX parsed skeleton contains no geoPoints")

    total_distance = 0.0
    for i in range(1, len(geo_points)):
        total_distance += _haversine(geo_points[i - 1], geo_points[i])

    for p in geo_points:
        if p.get("elevation") is None:
            p["elevation"] = 0.0

    lats = [p["latitude"] for p in geo_points]
    lons = [p["longitude"] for p in geo_points]

    bbox = {
        "center": {
            "latitude": (min(lats) + max(lats)) / 2,
            "longitude": (min(lons) + max(lons)) / 2,
        },
        "lowerLeft": {"latitude": min(lats), "longitude": min(lons)},
        "upperRight": {"latitude": max(lats), "longitude": max(lons)},
        "lowerLeftLatIsSet": True,
        "lowerLeftLongIsSet": True,
        "upperRightLatIsSet": True,
        "upperRightLongIsSet": True,
    }

    start_point = {
        "latitude": geo_points[0]["latitude"],
        "longitude": geo_points[0]["longitude"],
        "elevation": geo_points[0].get("elevation") or 0.0,
        "distance": None,
        "timestamp": None,
    }

    bearing = _initial_bearing(geo_points[0], geo_points[-1])

    return {
        "courseName": course_name,
        "description": description,
        "openStreetMap": False,
        "matchedToSegments": False,
        "userProfilePk": None,
        "userGroupPk": None,
        "rulePK": 2,  # private
        "geoRoutePk": None,
        "sourceTypeId": 3,  # GPX
        "sourcePk": None,
        "distanceMeter": total_distance,
        "elevationGainMeter": 0.0,
        "elevationLossMeter": 0.0,
        "startPoint": start_point,
        "coursePoints": [],
        "boundingBox": bbox,
        "hasShareableEvent": False,
        "hasTurnDetectionDisabled": False,
        "activityTypePk": activity_type_id,
        "virtualPartnerId": None,
        "includeLaps": False,
        "elapsedSeconds": None,
        "startBearing": bearing,
        "endBearing": None,
        "geoPoints": geo_points,
    }


def _resolve_gpx_output_path(course_id: int, output_path: Optional[str] = None) -> Optional[str]:
    """Resolve where to save a course GPX: output_path, else GARMIN_FIT_DOWNLOAD_DIR.

    Returns None when neither is given. There is no implicit default: on a hosted
    server the working directory may be read-only, and a file there is out of
    the user's reach anyway (the GPX is returned in the tool response).
    """
    if output_path:
        p = os.path.abspath(os.path.expanduser(output_path))
        if os.path.isdir(p) or output_path.endswith("/") or output_path.endswith("\\"):
            return os.path.join(p, f"{course_id}.gpx")
        return p

    env_dir = os.getenv("GARMIN_FIT_DOWNLOAD_DIR")
    if not env_dir:
        return None
    return os.path.join(os.path.abspath(os.path.expanduser(env_dir)), f"{course_id}.gpx")


def _as_list(value: Any) -> list:
    """Garmin sends null, not [], for an empty collection (e.g. a course with no waypoints)."""
    return value if isinstance(value, list) else []


def _course_point(p: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A geoPoint/startPoint as {lat, lon, elevation_m}, or None if it has no position."""
    if not p or p.get("latitude") is None or p.get("longitude") is None:
        return None
    return {
        "lat": round(p["latitude"], 6),
        "lon": round(p["longitude"], 6),
        "elevation_m": p.get("elevation"),
    }


def _start_finish(first: Optional[Dict[str, Any]], last: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """start/finish as {lat, lon, elevation_m} plus the straight-line gap between them (~0 for a loop)."""
    start, finish = _course_point(first), _course_point(last)
    gap = round(_haversine(first, last), 1) if start and finish else None
    return {"start": start, "finish": finish, "start_finish_gap_m": gap}


_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _gpx_point(trkpt) -> Dict[str, Any]:
    """A GPX <trkpt> element as a geoPoint-style dict."""
    ele = trkpt.findtext("gpx:ele", namespaces=_GPX_NS)
    return {
        "latitude": float(trkpt.get("lat")),
        "longitude": float(trkpt.get("lon")),
        "elevation": float(ele) if ele else None,
    }


def register_tools(app):
    """Register course management tools"""

    @app.tool()
    async def get_courses() -> str:
        """List all courses saved on Garmin Connect.

        Returns a curated list of courses with id, name, distance, activity type
        and creation date.
        """
        try:
            data = garmin_client.client.connectapi("/course-service/course")

            if not isinstance(data, list):
                return json.dumps(data, indent=2)

            curated = [
                {
                    "course_id": c.get("courseId"),
                    "name": c.get("courseName"),
                    "distance_m": c.get("distanceInMeters"),
                    "elevation_gain_m": c.get("elevationGainInMeters"),
                    "elevation_loss_m": c.get("elevationLossInMeters"),
                    "activity": (c.get("activityType") or {}).get("typeKey"),
                    "has_pace_band": c.get("hasPaceBand"),
                    "created": c.get("createdDateFormatted"),
                }
                for c in data
            ]
            return json.dumps({"count": len(curated), "courses": curated}, indent=2)
        except Exception as e:
            return f"Error listing courses: {str(e)}"

    @app.tool()
    async def get_course_details(course_id: int) -> str:
        """Get full details of a Garmin Connect course by ID.

        Returns where the course starts and finishes (first and last track
        points, lat/lon, plus the straight-line gap between them: near zero
        for a loop), total distance, elevation gain/loss, and any course
        waypoints (water, food, hazards, segment start/end, ...).

        To save a course's start or finish as a saved location on the user's
        watch, use get_course_location_share instead; don't build Google Maps
        or other web map links, which can't send a location to Garmin.

        Args:
            course_id: ID of the course (from get_courses).
        """
        try:
            data = garmin_client.client.connectapi(f"/course-service/course/{course_id}")
            if not isinstance(data, dict):
                return json.dumps(data, indent=2)

            course_points = [
                {
                    "name": cp.get("name"),
                    "type": cp.get("coursePointType"),
                    "lat": cp.get("lat"),
                    "lon": cp.get("lon"),
                    "distance_m": cp.get("distance"),
                }
                for cp in _as_list(data.get("coursePoints"))
            ]

            geo_points = _as_list(data.get("geoPoints"))
            first = geo_points[0] if geo_points else data.get("startPoint")
            last = geo_points[-1] if geo_points else None

            result = {
                "course_id": data.get("courseId"),
                "name": data.get("courseName"),
                "distance_m": data.get("distanceInMeters") or data.get("distanceMeter"),
                "elevation_gain_m": data.get("elevationGainInMeters") or data.get("elevationGainMeter"),
                "elevation_loss_m": data.get("elevationLossInMeters") or data.get("elevationLossMeter"),
                "activity": (data.get("activityType") or {}).get("typeKey"),
                "activity_type_id": data.get("activityTypePk"),
                **_start_finish(first, last),
                "waypoints_count": len(course_points),
                "waypoints": course_points,
                "geo_points_count": len(geo_points),
                "url": f"https://connect.{garmin_client.client.domain}/modern/course/{course_id}",
                "save_to_watch": (
                    "To save the start or finish as a saved location on the watch, call "
                    "get_course_location_share(course_id, point='start'|'finish'). Don't give "
                    "Google Maps or web map links: they can't send a location to Garmin."
                ),
            }
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error fetching course details: {str(e)}"

    @app.tool()
    async def download_course_gpx(
        course_id: int,
        output_path: Optional[str] = None,
        include_gpx: bool = False,
    ) -> str:
        """Download the exact official GPX file for a Garmin Connect course.

        Returns a summary of Garmin Connect's own GPX export: name, start and
        finish (first/last track point, lat/lon), the straight-line gap between
        them, and point counts. The GPX itself is included (as "gpx") only when
        include_gpx is true: it is large (a 5 km course is ~65 KB), so ask for it
        only when the full file is actually needed.

        The file is also saved to disk only when output_path is given or
        GARMIN_FIT_DOWNLOAD_DIR is set; a failed save is reported as "file_error".

        Args:
            course_id: ID of the course to download.
            output_path: Optional destination file or directory path on the
                machine running this server.
            include_gpx: Include the full GPX text in the response (default false).
        """
        try:
            gpx_bytes = garmin_client.client.download(f"/course-service/course/gpx/{course_id}")
            root = ET.fromstring(gpx_bytes)
            name = root.findtext("gpx:metadata/gpx:name", namespaces=_GPX_NS) or root.findtext(
                "gpx:trk/gpx:name", namespaces=_GPX_NS
            )
            trkpts = root.findall(".//gpx:trkpt", _GPX_NS)

            gpx_path, file_error = None, None
            target_path = _resolve_gpx_output_path(course_id, output_path)
            if target_path:
                try:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with open(target_path, "wb") as f:
                        f.write(gpx_bytes)
                    gpx_path = target_path
                except OSError as e:
                    file_error = str(e)

            result = {
                "status": "success",
                "course_id": course_id,
                "name": name,
                **_start_finish(
                    _gpx_point(trkpts[0]) if trkpts else None,
                    _gpx_point(trkpts[-1]) if trkpts else None,
                ),
                "waypoints_count": len(root.findall("gpx:wpt", _GPX_NS)),
                "track_points_count": len(trkpts),
                "size_bytes": len(gpx_bytes),
                "gpx_path": gpx_path,
            }
            if file_error:
                result["file_error"] = file_error
            if include_gpx:
                result["gpx"] = gpx_bytes.decode("utf-8")
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error downloading course GPX: {str(e)}"

    @app.tool()
    async def get_course_location_share(
        course_id: int,
        point: str = "start",
        name: Optional[str] = None,
    ) -> str:
        """Prepare a course's start or finish to save as a Saved Location on the user's Garmin watch.

        Use this when the user asks to save, send or add a course's start or
        finish (end) to their watch or saved locations. Garmin has no API for
        saved locations, so the location is saved from the user's iPhone: give
        them the name, the coordinates to paste into Apple Maps and the
        how_to_save steps from the response.

        Args:
            course_id: ID of the course (from get_courses).
            point: "start" or "finish" ("end" also means finish). Defaults to start.
            name: Name for the saved location. Defaults to "<course name> start"
                or "<course name> finish".
        """
        which = str(point).strip().lower()
        which = "finish" if which == "end" else which
        if which not in ("start", "finish"):
            return f"Error: point must be 'start' or 'finish' (or 'end'), got '{point}'."
        try:
            data = garmin_client.client.connectapi(f"/course-service/course/{course_id}")
            if not isinstance(data, dict):
                return json.dumps(data, indent=2)

            geo_points = _as_list(data.get("geoPoints"))
            if which == "start":
                location = _course_point(geo_points[0] if geo_points else data.get("startPoint"))
            else:
                location = _course_point(geo_points[-1]) if geo_points else None
            if location is None:
                return f"Error: course {course_id} has no {which} position (no track points)."

            course_name = data.get("courseName") or f"Course {course_id}"
            label = name or f"{course_name} {which}"
            lat, lon = location["lat"], location["lon"]
            return json.dumps(
                {
                    "course_id": data.get("courseId", course_id),
                    "course_name": course_name,
                    "point": which,
                    "name": label,
                    "lat": lat,
                    "lon": lon,
                    "elevation_m": location["elevation_m"],
                    "coordinates": f"{lat}, {lon}",
                    "apple_maps_url": f"maps://?ll={lat},{lon}&q={quote(label)}",
                    "present_as": (
                        f"Reply with the name \"{label}\" and the coordinates {lat}, {lon} on their own "
                        "in a code block so the user can copy them, then the how_to_save steps. "
                        "Do not give Google Maps or web map links: only the iPhone Maps app can "
                        "share a location to Garmin Connect."
                    ),
                    "how_to_save": [
                        "Keep the watch connected to the iPhone over Bluetooth.",
                        f"Open the Maps app and paste {lat}, {lon} into the search bar "
                        "(or tap apple_maps_url on the iPhone; a web link opens a browser that can't share to Garmin Connect).",
                        "Tap the pin, then Share > Garmin Connect, and choose the watch if asked.",
                        f"It appears in the watch's Saved app; rename it to \"{label}\" there if it "
                        "arrives named by its coordinates or address.",
                    ],
                },
                indent=2,
            )
        except Exception as e:
            return f"Error getting course location: {str(e)}"

    @app.tool()
    async def upload_course(
        gpx_path: str,
        course_name: Optional[str] = None,
        activity_type: str = "running",
        description: Optional[str] = None,
    ) -> str:
        """Upload a GPX file as a Garmin Connect Course.

        The course can then be loaded onto the watch (sync or "Send to Device")
        and used as a navigation course or to build a PacePro strategy.

        Args:
            gpx_path: Absolute path to the .gpx file on disk.
            course_name: Override the course name. Defaults to the name parsed
                from the GPX file.
            activity_type: One of running, cycling, hiking, walking, trail_running,
                mountain_biking, road_biking, gravel_cycling. Defaults to running.
            description: Optional description shown on the course detail page.
        """
        try:
            _p = pathlib.Path(gpx_path)
            if _p.suffix.lower() != ".gpx":
                return f"Error: only .gpx files are allowed, got: {_p.suffix or '(no extension)'}"
            gpx_path = str(_p.resolve())
            if not os.path.isfile(gpx_path):
                return f"Error: GPX file not found: {gpx_path}"

            activity_type_id = _ACTIVITY_TYPE_IDS.get(activity_type.lower())
            if activity_type_id is None:
                return (
                    f"Error: unknown activity_type '{activity_type}'. "
                    f"Supported: {', '.join(sorted(_ACTIVITY_TYPE_IDS))}."
                )

            with open(gpx_path, "rb") as f:
                gpx_bytes = f.read()

            # Step 1: parse the GPX server-side
            parsed = garmin_client.client.post(
                "connectapi",
                "/course-service/course/import",
                files={
                    "file": (
                        os.path.basename(gpx_path),
                        io.BytesIO(gpx_bytes),
                        "application/gpx+xml",
                    )
                },
                api=True,
            )

            effective_name = (
                course_name
                or parsed.get("courseName")
                or os.path.splitext(os.path.basename(gpx_path))[0]
            )

            # Step 2: build the create payload and save
            payload = _build_course_payload(
                parsed,
                course_name=effective_name,
                activity_type_id=activity_type_id,
                description=description,
            )

            saved = garmin_client.client.post(
                "connectapi", "/course-service/course", json=payload, api=True,
            )
            return json.dumps(
                {
                    "status": "success",
                    "course_id": saved.get("courseId"),
                    "name": saved.get("courseName"),
                    "distance_m": saved.get("distanceMeter"),
                    "elevation_gain_m": saved.get("elevationGainMeter"),
                    "elevation_loss_m": saved.get("elevationLossMeter"),
                    "activity_type_id": saved.get("activityTypePk"),
                    "url": f"https://connect.{garmin_client.client.domain}/modern/course/{saved.get('courseId')}",
                },
                indent=2,
            )

        except Exception as e:
            return f"Error uploading course: {str(e)}"

    @app.tool()
    async def delete_course(course_id: int) -> str:
        """Delete a course from Garmin Connect.

        Args:
            course_id: ID of the course to delete (get IDs from get_courses).
        """
        try:
            garmin_client.client.delete(
                "connectapi", f"/course-service/course/{course_id}"
            )
            return json.dumps(
                {
                    "status": "success",
                    "course_id": course_id,
                    "message": f"Course {course_id} deleted",
                },
                indent=2,
            )
        except Exception as e:
            return f"Error deleting course: {str(e)}"

    return app
