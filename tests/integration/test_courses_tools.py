"""
Integration tests for the courses module MCP tools.

Covers get_courses, get_course_details, download_course_gpx, upload_course, and delete_course
using FastMCP integration with a mocked Garmin client. No real Garmin account or network access is used.
"""
import json
import os

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import courses
from garmin_mcp.courses import _build_course_payload, _haversine, _resolve_gpx_output_path
from tests.fixtures.garmin_responses import (
    MOCK_COURSE_DETAIL,
    MOCK_COURSE_DETAIL_NO_GEOMETRY,
    MOCK_COURSE_DETAIL_WITH_WAYPOINTS,
    MOCK_COURSE_GPX,
)


@pytest.fixture
def app_with_courses(mock_garmin_client):
    """Create a FastMCP app with the courses tools registered."""
    courses.configure(mock_garmin_client)
    app = FastMCP("Test Courses")
    app = courses.register_tools(app)
    return app


def _result_text(result):
    """Extract the text payload from a FastMCP call_tool result."""
    return result[0][0].text


# --- get_courses ----------------------------------------------------------

@pytest.mark.asyncio
async def test_get_courses_curates_fields(app_with_courses, mock_garmin_client):
    """get_courses curates the raw Garmin list into a compact shape."""
    mock_garmin_client.client.connectapi.return_value = [
        {
            "courseId": 111,
            "courseName": "River Loop",
            "distanceInMeters": 10250.5,
            "elevationGainInMeters": 120.0,
            "elevationLossInMeters": 118.0,
            "activityType": {"typeKey": "running"},
            "hasPaceBand": False,
            "createdDateFormatted": "2024-03-01",
        }
    ]

    result = await app_with_courses.call_tool("get_courses", {})

    data = json.loads(_result_text(result))
    assert data["count"] == 1
    course = data["courses"][0]
    assert course["course_id"] == 111
    assert course["name"] == "River Loop"
    assert course["distance_m"] == 10250.5
    assert course["activity"] == "running"
    mock_garmin_client.client.connectapi.assert_called_once_with("/course-service/course")


@pytest.mark.asyncio
async def test_get_courses_empty(app_with_courses, mock_garmin_client):
    """An empty course list returns count 0 and an empty list."""
    mock_garmin_client.client.connectapi.return_value = []

    result = await app_with_courses.call_tool("get_courses", {})

    data = json.loads(_result_text(result))
    assert data["count"] == 0
    assert data["courses"] == []


@pytest.mark.asyncio
async def test_get_courses_error_is_caught(app_with_courses, mock_garmin_client):
    """A client error is surfaced as a clean message, not a traceback."""
    mock_garmin_client.client.connectapi.side_effect = Exception("boom")

    result = await app_with_courses.call_tool("get_courses", {})

    assert "Error listing courses" in _result_text(result)


# --- get_course_details ---------------------------------------------------

@pytest.mark.asyncio
async def test_get_course_details_null_course_points(app_with_courses, mock_garmin_client):
    """Garmin's real shape (coursePoints: null) no longer crashes: no waypoints."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    text = _result_text(result)
    assert not text.startswith("Error"), text
    data = json.loads(text)
    assert data["waypoints_count"] == 0
    assert data["waypoints"] == []
    assert data["geo_points_count"] == 3
    mock_garmin_client.client.connectapi.assert_called_once_with(
        "/course-service/course/700000001"
    )


@pytest.mark.asyncio
async def test_get_course_details_start_and_finish(app_with_courses, mock_garmin_client):
    """Start and finish are the first and last track points, plus the gap between them."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert data["start"] == {"lat": 51.5, "lon": -0.1, "elevation_m": 20.5}
    assert data["finish"] == {"lat": 51.501, "lon": -0.1, "elevation_m": 21.25}
    # 0.001 degrees of latitude on a 6371 km sphere.
    assert data["start_finish_gap_m"] == 111.2


@pytest.mark.asyncio
async def test_get_course_details_metadata(app_with_courses, mock_garmin_client):
    """Distance, elevation and activity type come from the detail payload's own keys."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert data["course_id"] == 700000001
    assert data["name"] == "Riverside 5K"
    assert data["distance_m"] == 5132.19
    assert data["elevation_gain_m"] == 65.58
    assert data["elevation_loss_m"] == 60.1
    assert data["activity_type_id"] == 1


@pytest.mark.asyncio
async def test_get_course_details_waypoints(app_with_courses, mock_garmin_client):
    """Waypoints are listed with their coursePointType."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL_WITH_WAYPOINTS

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert data["waypoints_count"] == 1
    assert data["waypoints"] == [
        {"name": "Water", "type": "WATER", "lat": 51.5005, "lon": -0.1, "distance_m": 55.6}
    ]


@pytest.mark.asyncio
async def test_get_course_details_without_geometry(app_with_courses, mock_garmin_client):
    """Missing coursePoints and null geoPoints: start falls back to startPoint, no finish."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL_NO_GEOMETRY

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    text = _result_text(result)
    assert not text.startswith("Error"), text
    data = json.loads(text)
    assert data["start"] == {"lat": 51.5, "lon": -0.1, "elevation_m": 20.5}
    assert data["finish"] is None
    assert data["start_finish_gap_m"] is None
    assert data["geo_points_count"] == 0
    assert data["waypoints"] == []


@pytest.mark.asyncio
async def test_get_course_details_error_is_caught(app_with_courses, mock_garmin_client):
    """Client error in get_course_details is caught cleanly."""
    mock_garmin_client.client.connectapi.side_effect = Exception("Not found")

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 999})

    assert "Error fetching course details" in _result_text(result)


# --- download_course_gpx ---------------------------------------------------

@pytest.fixture
def no_download_dir(monkeypatch, tmp_path):
    """No download directory configured; the server runs in an empty directory."""
    monkeypatch.delenv("GARMIN_FIT_DOWNLOAD_DIR", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.mark.asyncio
async def test_download_course_gpx_summary_by_default(
    app_with_courses, mock_garmin_client, no_download_dir
):
    """By default only a summary comes back (no GPX body), and nothing is written to disk."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert data["status"] == "success"
    assert data["course_id"] == 700000001
    assert data["name"] == "Riverside 5K"
    assert data["track_points_count"] == 3
    assert data["waypoints_count"] == 1
    assert data["size_bytes"] == len(MOCK_COURSE_GPX)
    assert data["gpx_path"] is None
    assert "gpx" not in data
    mock_garmin_client.client.download.assert_called_once_with(
        "/course-service/course/gpx/700000001"
    )
    assert list(no_download_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_download_course_gpx_start_and_finish(
    app_with_courses, mock_garmin_client, no_download_dir
):
    """The summary gives start and finish from the first and last GPX track points."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert data["start"] == {"lat": 51.5, "lon": -0.1, "elevation_m": 20.5}
    assert data["finish"] == {"lat": 51.501, "lon": -0.1, "elevation_m": 21.25}
    assert data["start_finish_gap_m"] == 111.2


@pytest.mark.asyncio
async def test_download_course_gpx_without_track(
    app_with_courses, mock_garmin_client, no_download_dir
):
    """A GPX with no track points has no start or finish, rather than an error."""
    mock_garmin_client.client.download.return_value = (
        b'<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">'
        b"<metadata><name>Empty</name></metadata></gpx>"
    )

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 1})

    data = json.loads(_result_text(result))
    assert data["name"] == "Empty"
    assert data["track_points_count"] == 0
    assert data["start"] is None
    assert data["finish"] is None
    assert data["start_finish_gap_m"] is None


@pytest.mark.asyncio
async def test_download_course_gpx_include_gpx(
    app_with_courses, mock_garmin_client, no_download_dir
):
    """include_gpx=True returns Garmin's export unchanged in the response."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX

    result = await app_with_courses.call_tool(
        "download_course_gpx", {"course_id": 700000001, "include_gpx": True}
    )

    data = json.loads(_result_text(result))
    assert data["gpx"] == MOCK_COURSE_GPX.decode("utf-8")
    assert data["track_points_count"] == 3


@pytest.mark.asyncio
async def test_download_course_gpx_writes_output_path(
    app_with_courses, mock_garmin_client, tmp_path
):
    """An explicit output_path gets Garmin's bytes unchanged; the response stays a summary."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX
    out_file = tmp_path / "riverside.gpx"

    result = await app_with_courses.call_tool(
        "download_course_gpx", {"course_id": 700000001, "output_path": str(out_file)}
    )

    data = json.loads(_result_text(result))
    assert data["gpx_path"] == str(out_file)
    assert out_file.read_bytes() == MOCK_COURSE_GPX
    assert "gpx" not in data


@pytest.mark.asyncio
async def test_download_course_gpx_env_dir(
    app_with_courses, mock_garmin_client, tmp_path, monkeypatch
):
    """With GARMIN_FIT_DOWNLOAD_DIR set, the file is saved there as {course_id}.gpx."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX
    monkeypatch.setenv("GARMIN_FIT_DOWNLOAD_DIR", str(tmp_path / "gpx"))

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    expected = tmp_path / "gpx" / "700000001.gpx"
    assert data["gpx_path"] == str(expected)
    assert expected.read_bytes() == MOCK_COURSE_GPX


@pytest.mark.asyncio
async def test_download_course_gpx_unwritable_path(
    app_with_courses, mock_garmin_client, tmp_path
):
    """A path that can't be written (as on a read-only host) is reported, not fatal."""
    mock_garmin_client.client.download.return_value = MOCK_COURSE_GPX
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    result = await app_with_courses.call_tool(
        "download_course_gpx",
        {"course_id": 700000001, "output_path": str(blocker / "course.gpx")},
    )

    data = json.loads(_result_text(result))
    assert data["status"] == "success"
    assert data["gpx_path"] is None
    assert data["file_error"]
    assert data["start"] == {"lat": 51.5, "lon": -0.1, "elevation_m": 20.5}


@pytest.mark.asyncio
async def test_download_course_gpx_not_gpx(app_with_courses, mock_garmin_client, no_download_dir):
    """A body that isn't GPX is reported as an error, not returned as a course."""
    mock_garmin_client.client.download.return_value = b'{"message": "Not Found"}'

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 404})

    assert "Error downloading course GPX" in _result_text(result)


@pytest.mark.asyncio
async def test_download_course_gpx_error_is_caught(
    app_with_courses, mock_garmin_client, no_download_dir
):
    """A Garmin error (e.g. unknown course) is surfaced as a clean message."""
    mock_garmin_client.client.download.side_effect = Exception("API Error 404")

    result = await app_with_courses.call_tool("download_course_gpx", {"course_id": 404})

    assert "Error downloading course GPX: API Error 404" in _result_text(result)


# --- upload_course --------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_course_rejects_non_gpx(app_with_courses, mock_garmin_client):
    """Only .gpx files are accepted; nothing is uploaded otherwise."""
    result = await app_with_courses.call_tool(
        "upload_course", {"gpx_path": "/tmp/route.tcx"}
    )

    assert "only .gpx files are allowed" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_upload_course_missing_file(app_with_courses, mock_garmin_client):
    """A missing file path returns an error message before hitting Garmin."""
    result = await app_with_courses.call_tool(
        "upload_course", {"gpx_path": "/tmp/nonexistent-route-12345.gpx"}
    )

    assert "GPX file not found" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_upload_course_rejects_unknown_activity_type(
    app_with_courses, mock_garmin_client, tmp_path
):
    """Unknown activity_type strings are rejected with supported list."""
    gpx_file = tmp_path / "valid.gpx"
    gpx_file.write_text("<gpx></gpx>")

    result = await app_with_courses.call_tool(
        "upload_course",
        {"gpx_path": str(gpx_file), "activity_type": "paragliding"},
    )

    assert "unknown activity_type 'paragliding'" in _result_text(result)
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_upload_course_success(app_with_courses, mock_garmin_client, tmp_path):
    """Valid GPX file triggers step 1 import and step 2 save, returning metadata."""
    gpx_file = tmp_path / "test.gpx"
    gpx_file.write_text("<gpx><trk><trkseg></trkseg></trk></gpx>")

    mock_garmin_client.client.post.side_effect = [
        # Step 1: /course-service/course/import response skeleton
        {
            "courseName": "River Loop",
            "geoPoints": [
                {"latitude": 40.0, "longitude": -105.0, "elevation": 1600.0},
                {"latitude": 40.01, "longitude": -105.01, "elevation": 1610.0},
            ],
        },
        # Step 2: /course-service/course save response
        {
            "courseId": 999,
            "courseName": "River Loop",
            "distanceMeter": 1250.0,
            "elevationGainMeter": 10.0,
            "elevationLossMeter": 0.0,
            "activityTypePk": 1,
        },
    ]

    result = await app_with_courses.call_tool(
        "upload_course",
        {
            "gpx_path": str(gpx_file),
            "course_name": "River Loop",
            "activity_type": "running",
        },
    )

    data = json.loads(_result_text(result))
    assert data["status"] == "success"
    assert data["course_id"] == 999
    assert data["name"] == "River Loop"
    assert data["activity_type_id"] == 1
    assert "course/999" in data["url"]

    # Two-step flow: POST /import then POST /course.
    assert mock_garmin_client.client.post.call_count == 2
    import_call, create_call = mock_garmin_client.client.post.call_args_list
    assert import_call.args[1] == "/course-service/course/import"
    assert create_call.args[1] == "/course-service/course"


# --- delete_course --------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_course_success(app_with_courses, mock_garmin_client):
    """delete_course hits the right endpoint and reports success."""
    result = await app_with_courses.call_tool("delete_course", {"course_id": 555})

    data = json.loads(_result_text(result))
    assert data["status"] == "success"
    assert data["course_id"] == 555
    mock_garmin_client.client.delete.assert_called_once_with(
        "connectapi", "/course-service/course/555"
    )


@pytest.mark.asyncio
async def test_delete_course_error_is_caught(app_with_courses, mock_garmin_client):
    """A client error is surfaced as a clean message, not a traceback."""
    mock_garmin_client.client.delete.side_effect = Exception("nope")

    result = await app_with_courses.call_tool("delete_course", {"course_id": 555})

    assert "Error deleting course" in _result_text(result)


# --- pure helpers ---------------------------------------------------------

def test_build_course_payload_computes_distance_and_defaults():
    """Distances accumulate, missing elevation defaults to 0, bbox is derived."""
    parsed = {
        "geoPoints": [
            {"latitude": 40.0, "longitude": -105.0, "elevation": 1600.0},
            {"latitude": 40.0, "longitude": -105.0, "elevation": None},
        ],
    }

    payload = _build_course_payload(parsed, "X", 1, None)

    assert payload["courseName"] == "X"
    assert payload["activityTypePk"] == 1
    # Identical points -> zero total distance.
    assert payload["distanceMeter"] == 0.0
    # Missing elevation is backfilled to 0.0.
    assert payload["geoPoints"][1]["elevation"] == 0.0
    assert payload["boundingBox"]["lowerLeft"]["latitude"] == 40.0


def test_build_course_payload_rejects_too_few_points():
    """A GPX skeleton without geoPoints raises ValueError."""
    with pytest.raises(ValueError, match="no geoPoints"):
        _build_course_payload({}, "X", 1, None)


def test_resolve_gpx_output_path(monkeypatch):
    """Output path resolution precedence and directory handling."""
    monkeypatch.delenv("GARMIN_FIT_DOWNLOAD_DIR", raising=False)

    # 1. Custom file path
    assert _resolve_gpx_output_path(123, "/tmp/custom.gpx") == "/tmp/custom.gpx"

    # 2. Custom directory path
    assert _resolve_gpx_output_path(123, "/tmp/dir/") == "/tmp/dir/123.gpx"

    # 3. Nothing configured: no file (the server's working directory may be read-only)
    assert _resolve_gpx_output_path(123) is None

    # 4. GARMIN_FIT_DOWNLOAD_DIR
    monkeypatch.setenv("GARMIN_FIT_DOWNLOAD_DIR", "/tmp/gpx")
    assert _resolve_gpx_output_path(123) == "/tmp/gpx/123.gpx"


# --- get_course_location_share ------------------------------------------

@pytest.mark.asyncio
async def test_course_location_share_start(app_with_courses, mock_garmin_client):
    """The start comes back ready to save on the watch via Apple Maps."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001}
    )

    data = json.loads(_result_text(result))
    assert data["course_id"] == 700000001
    assert data["course_name"] == "Riverside 5K"
    assert data["point"] == "start"
    assert data["name"] == "Riverside 5K start"
    assert data["lat"] == 51.5
    assert data["lon"] == -0.1
    assert data["elevation_m"] == 20.5
    assert data["coordinates"] == "51.5, -0.1"
    assert data["apple_maps_url"] == "maps://?ll=51.5,-0.1&q=Riverside%205K%20start"
    assert data["how_to_save"]
    mock_garmin_client.client.connectapi.assert_called_once_with(
        "/course-service/course/700000001"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["finish", "end", "Finish"])
async def test_course_location_share_finish(app_with_courses, mock_garmin_client, point):
    """finish (or end) is the last track point."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001, "point": point}
    )

    data = json.loads(_result_text(result))
    assert data["point"] == "finish"
    assert data["name"] == "Riverside 5K finish"
    assert data["coordinates"] == "51.501, -0.1"
    assert data["elevation_m"] == 21.25


@pytest.mark.asyncio
async def test_course_location_share_custom_name(app_with_courses, mock_garmin_client):
    """A given name is used and URL-encoded in the link."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool(
        "get_course_location_share",
        {"course_id": 700000001, "name": "Car park & café"},
    )

    data = json.loads(_result_text(result))
    assert data["name"] == "Car park & café"
    assert data["apple_maps_url"] == "maps://?ll=51.5,-0.1&q=Car%20park%20%26%20caf%C3%A9"


@pytest.mark.asyncio
async def test_course_location_share_rejects_unknown_point(app_with_courses, mock_garmin_client):
    """Anything other than start/finish/end is refused before calling Garmin."""
    result = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001, "point": "middle"}
    )

    assert "point must be" in _result_text(result)
    mock_garmin_client.client.connectapi.assert_not_called()


@pytest.mark.asyncio
async def test_course_location_share_without_track(app_with_courses, mock_garmin_client):
    """No track points: the start falls back to startPoint, the finish is an error."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL_NO_GEOMETRY

    start = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001}
    )
    finish = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001, "point": "finish"}
    )

    assert json.loads(_result_text(start))["coordinates"] == "51.5, -0.1"
    assert "no finish" in _result_text(finish)


@pytest.mark.asyncio
async def test_course_location_share_error_is_caught(app_with_courses, mock_garmin_client):
    """A Garmin error is surfaced as a clean message."""
    mock_garmin_client.client.connectapi.side_effect = Exception("API Error 404")

    result = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 404}
    )

    assert "Error getting course location: API Error 404" in _result_text(result)


@pytest.mark.asyncio
async def test_get_course_details_points_to_location_share(app_with_courses, mock_garmin_client):
    """Course details steer saving a start/finish on the watch to get_course_location_share."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool("get_course_details", {"course_id": 700000001})

    data = json.loads(_result_text(result))
    assert "get_course_location_share" in data["save_to_watch"]


@pytest.mark.asyncio
async def test_course_location_share_says_how_to_present(app_with_courses, mock_garmin_client):
    """The share response tells Claude to show the copyable coordinates, not a web map link."""
    mock_garmin_client.client.connectapi.return_value = MOCK_COURSE_DETAIL

    result = await app_with_courses.call_tool(
        "get_course_location_share", {"course_id": 700000001}
    )

    data = json.loads(_result_text(result))
    assert "51.5, -0.1" in data["present_as"]
    assert "Google Maps" in data["present_as"]
