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


_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


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
            start, finish = _course_point(first), _course_point(last)
            gap = round(_haversine(first, last), 1) if start and finish else None

            result = {
                "course_id": data.get("courseId"),
                "name": data.get("courseName"),
                "distance_m": data.get("distanceInMeters") or data.get("distanceMeter"),
                "elevation_gain_m": data.get("elevationGainInMeters") or data.get("elevationGainMeter"),
                "elevation_loss_m": data.get("elevationLossInMeters") or data.get("elevationLossMeter"),
                "activity": (data.get("activityType") or {}).get("typeKey"),
                "activity_type_id": data.get("activityTypePk"),
                "start": start,
                "finish": finish,
                "start_finish_gap_m": gap,
                "waypoints_count": len(course_points),
                "waypoints": course_points,
                "geo_points_count": len(geo_points),
                "url": f"https://connect.{garmin_client.client.domain}/modern/course/{course_id}",
            }
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error fetching course details: {str(e)}"

    @app.tool()
    async def download_course_gpx(
        course_id: int,
        output_path: Optional[str] = None,
    ) -> str:
        """Download the exact official GPX file for a Garmin Connect course.

        The GPX (Garmin Connect's own export) is returned in the response as
        "gpx", with a summary. It is also saved to disk only when output_path is
        given or GARMIN_FIT_DOWNLOAD_DIR is set; a failed save is reported as
        "file_error" and the GPX is still returned.

        Args:
            course_id: ID of the course to download.
            output_path: Optional destination file or directory path on the
                machine running this server.
        """
        try:
            gpx_bytes = garmin_client.client.download(f"/course-service/course/gpx/{course_id}")
            root = ET.fromstring(gpx_bytes)
            name = root.findtext("gpx:metadata/gpx:name", namespaces=_GPX_NS) or root.findtext(
                "gpx:trk/gpx:name", namespaces=_GPX_NS
            )

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
                "waypoints_count": len(root.findall("gpx:wpt", _GPX_NS)),
                "track_points_count": len(root.findall(".//gpx:trkpt", _GPX_NS)),
                "size_bytes": len(gpx_bytes),
                "gpx_path": gpx_path,
            }
            if file_error:
                result["file_error"] = file_error
            result["gpx"] = gpx_bytes.decode("utf-8")
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error downloading course GPX: {str(e)}"

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
