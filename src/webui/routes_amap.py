"""AMap (高德地图) API proxy endpoints for the Web UI."""

import json

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cyrene.config import AMAP_API_KEY


def register_amap_routes(router: APIRouter) -> None:
    @router.get("/api/amap/verify")
    async def verify_amap_key():
        """Test whether the currently saved AMAP_API_KEY is valid."""
        if not AMAP_API_KEY:
            return {"valid": False, "error": "Key 未配置"}
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    "https://restapi.amap.com/v3/direction/driving",
                    params={
                        "key": AMAP_API_KEY,
                        "origin": "116.4,39.9",
                        "destination": "116.5,39.9",
                    },
                    timeout=10,
                )
            data = r.json()
            if data.get("status") == "1":
                return {"valid": True}
            return {"valid": False, "error": data.get("info", "验证失败")}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    @router.get("/api/amap/direction")
    async def amap_direction(
        fromLng: float, fromLat: float, toLng: float, toLat: float, profile: str = "driving"
    ):
        """Proxy direction request to AMap Web API, keeping the key server-side."""
        profile_map = {"driving": "driving", "walking": "walking", "cycling": "bicycling"}
        amap_profile = profile_map.get(profile, "driving")
        try:
            async with httpx.AsyncClient() as c:
                resp = await c.get(
                    f"https://restapi.amap.com/v3/direction/{amap_profile}",
                    params={
                        "key": AMAP_API_KEY,
                        "origin": f"{fromLng},{fromLat}",
                        "destination": f"{toLng},{toLat}",
                        "extensions": "base",
                        "strategy": "0",
                    },
                    timeout=15,
                )
            data = resp.json()
            if data.get("status") != "1":
                return JSONResponse(
                    {"error": data.get("info", "路线请求失败")}, status_code=502
                )
            # Extract route polyline coordinates from steps.
            coords: list[list[float]] = []
            for step in data["route"]["paths"][0]["steps"]:
                for point in step["polyline"].split(";"):
                    lng_s, lat_s = point.split(",")
                    coords.append([float(lng_s), float(lat_s)])
            return {"coordinates": coords}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
