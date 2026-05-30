// Map view for the right sidebar — Leaflet-based interactive map.
// Supports default (CartoDB + straight lines) and AMap (高德 tiles + real routes).
const { useState, useEffect, useRef } = React;
const renderMarkdown = window.renderMarkdown || function (t) { return String(t || ""); };
var _escapeHtml = function (s) { return String(s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); };
var _normMd = function (s) { return String(s || "").replace(/\\n/g, "\n"); };

// WGS-84 → GCJ-02 (火星坐标系) conversion for AMap tiles.
var _pi = 3.1415926535897932384626;
var _a = 6378245.0;
var _ee = 0.00669342162296594323;
function _transformLat(x, y) { var t = x; var ret = -100.0 + 2.0 * t + 3.0 * y + 0.2 * y * y + 0.1 * t * y + 0.2 * Math.sqrt(Math.abs(t)); ret += (20.0 * Math.sin(6.0 * t * _pi) + 20.0 * Math.sin(2.0 * t * _pi)) * 2.0 / 3.0; ret += (20.0 * Math.sin(y * _pi) + 40.0 * Math.sin(y / 3.0 * _pi)) * 2.0 / 3.0; ret += (160.0 * Math.sin(y / 12.0 * _pi) + 320.0 * Math.sin(y * _pi / 30.0)) * 2.0 / 3.0; return ret; }
function _transformLng(x, y) { var t = x; var ret = 300.0 + t + 2.0 * y + 0.1 * t * t + 0.1 * t * y + 0.1 * Math.sqrt(Math.abs(t)); ret += (20.0 * Math.sin(6.0 * t * _pi) + 20.0 * Math.sin(2.0 * t * _pi)) * 2.0 / 3.0; ret += (20.0 * Math.sin(t * _pi) + 40.0 * Math.sin(t / 3.0 * _pi)) * 2.0 / 3.0; ret += (150.0 * Math.sin(t / 12.0 * _pi) + 300.0 * Math.sin(t / 30.0 * _pi)) * 2.0 / 3.0; return ret; }
function wgs84ToGcj02(wgsLat, wgsLng) {
  // Skip conversion for coordinates far outside China (rough boundary check).
  if (wgsLng < 72.004 || wgsLng > 137.8347 || wgsLat < 0.8293 || wgsLat > 55.8271) return [wgsLat, wgsLng];
  var dlat = _transformLat(wgsLng - 105.0, wgsLat - 35.0);
  var dlng = _transformLng(wgsLng - 105.0, wgsLat - 35.0);
  var radlat = wgsLat / 180.0 * _pi;
  var magic = Math.sin(radlat);
  magic = 1 - _ee * magic * magic;
  var sqrtmagic = Math.sqrt(magic);
  dlat = (dlat * 180.0) / ((_a * (1 - _ee)) / (magic * sqrtmagic) * _pi);
  dlng = (dlng * 180.0) / (_a / sqrtmagic * Math.cos(radlat) * _pi);
  return [wgsLat + dlat, wgsLng + dlng];
}

function getMapProvider() { try { return localStorage.getItem("cyrene-tweak-map-provider") || "direct"; } catch(e) { return "direct"; } }

function getTileUrl(isDark, provider) {
  if (provider === "amap") {
    return isDark
      ? "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
      : "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}";
  }
  return isDark
    ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
    : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
}
function getTileAttribution(provider) {
  return provider === "amap"
    ? '&copy; <a href="https://console.amap.com/">高德</a>'
    : '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, &copy; <a href="https://carto.com/">CARTO</a>';
}

// Route cache for AMap direction responses.
var _routeCache = {};

function MapView() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const layerRef = useRef(null);
  const [pins, setPins] = useState([]);
  const [routes, setRoutes] = useState([]);

  // Load existing data from API on mount.
  useEffect(function () {
    fetch("/api/map/pins")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.pins) setPins(data.pins);
        if (data && data.routes) setRoutes(data.routes);
      })
      .catch(function () {});
  }, []);

  // Subscribe to SSE updates via the global DATA object.
  useEffect(function () {
    var timer = setInterval(function () {
      var dp = DATA.map && DATA.map.pins;
      var dr = DATA.map && DATA.map.routes;
      if (dp) setPins(dp);
      if (dr) setRoutes(dr);
    }, 500);
    return function () { clearInterval(timer); };
  }, []);

  // Initialize Leaflet map (once per component mount).
  useEffect(function () {
    if (mapRef.current || !containerRef.current || !window.L) return;
    try {
      var L = window.L;
      var el = containerRef.current;
      var provider = getMapProvider();
      var isDark = isDarkTheme();
      var tileUrl = getTileUrl(isDark, provider);

      var map = L.map(el, {
        zoomControl: false,
        attributionControl: true,
      }).setView([35, 105], 4);

      L.tileLayer(tileUrl, {
        attribution: getTileAttribution(provider),
        subdomains: provider === "amap" ? [] : "abcd",
      }).addTo(map);
      mapRef.current = map;
      layerRef.current = L.layerGroup().addTo(map);

      setTimeout(function () { try { map.invalidateSize(); } catch (_e) {} }, 100);
    } catch (e) {
      console.warn("MapView init error:", e);
    }

    return function () {
      if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; layerRef.current = null; }
    };
  }, []);

  function pinLookup() {
    var m = {};
    (pins || []).forEach(function (p) { if (p.name) m[p.name] = p; });
    return m;
  }

  // Re-render markers + routes when pins or routes change.
  useEffect(function () {
    var map = mapRef.current;
    var layer = layerRef.current;
    if (!map || !layer || !window.L) return;
    var L = window.L;

    layer.clearLayers();
    if ((!pins || pins.length === 0) && (!routes || routes.length === 0)) return;

    var provider = getMapProvider();
    var useAmap = provider === "amap";
    var lookup = pinLookup();
    var latlngs = [];

    // Draw markers for each pin.
    (pins || []).forEach(function (pin) {
      if (pin.lat == null || pin.lng == null) return;
      var ll = useAmap ? wgs84ToGcj02(pin.lat, pin.lng) : [pin.lat, pin.lng];
      latlngs.push(ll);

      var popupHtml = "";
      if (pin.name) popupHtml += "<b>" + _escapeHtml(pin.name) + "</b>";
      if (pin.note_md) {
        var noteHtml = renderMarkdown(_normMd(pin.note_md));
        if (noteHtml) popupHtml += "<br>" + noteHtml;
      }
      if (!popupHtml) popupHtml = pin.lat.toFixed(4) + ", " + pin.lng.toFixed(4);

      var marker = L.marker(ll, { title: pin.name || "" });
      marker.bindPopup(popupHtml, { maxWidth: 300 });
      layer.addLayer(marker);
    });

    // Helper: draw a route line with wide invisible hit area.
    var lineColor = isDarkTheme() ? "#6ea8fe" : "#0d6efd";
    function addRouteLine(fromLl, toLl, popupHtml, routeProfile) {
      if (useAmap) {
        // AMap mode: fetch real route from server proxy.
        var cacheKey = fromLl[0].toFixed(5) + "," + fromLl[1].toFixed(5) + ":" + toLl[0].toFixed(5) + "," + toLl[1].toFixed(5) + ":" + (routeProfile || "driving");
        var cached = _routeCache[cacheKey];
        if (cached) {
          drawActualRoute(cached, popupHtml);
          return;
        }
        // Draw placeholder straight line while fetching.
        var placeholder = drawStraightRoute(fromLl, toLl, popupHtml, true);
        var amapProfile = profileFromTransport(popupHtml);
        var fp = useAmap ? wgs84ToGcj02(fromLl[0], fromLl[1]) : fromLl;
        var tp = useAmap ? wgs84ToGcj02(toLl[0], toLl[1]) : toLl;
        fetch("/api/amap/direction?fromLng=" + fp[1] + "&fromLat=" + fp[0] + "&toLng=" + tp[1] + "&toLat=" + tp[0] + "&profile=" + amapProfile)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data && data.coordinates && data.coordinates.length > 0) {
              _routeCache[cacheKey] = data.coordinates;
              layer.removeLayer(placeholder);
              drawActualRoute(data.coordinates, popupHtml);
            }
          })
          .catch(function () {});
      } else {
        drawStraightRoute(fromLl, toLl, popupHtml, false);
      }
    }

    function drawStraightRoute(fromLl, toLl, popupHtml, isPlaceholder) {
      var hit = L.polyline([fromLl, toLl], {
        weight: 20, opacity: 0, interactive: true,
      });
      if (popupHtml) hit.bindPopup(popupHtml, { maxWidth: 250 });
      layer.addLayer(hit);
      var vis = L.polyline([fromLl, toLl], {
        color: lineColor, weight: isPlaceholder ? 2 : 6, opacity: isPlaceholder ? 0.3 : 0.7, dashArray: isPlaceholder ? "4, 4" : "8, 6",
        interactive: false,
      });
      layer.addLayer(vis);
      return vis;
    }

    function drawActualRoute(coords, popupHtml) {
      var ll = coords.map(function (c) { return [c[1], c[0]]; }); // GeoJSON [lng,lat] → Leaflet [lat,lng]
      var hit = L.polyline(ll, {
        weight: 20, opacity: 0, interactive: true,
      });
      if (popupHtml) hit.bindPopup(popupHtml, { maxWidth: 250 });
      layer.addLayer(hit);
      L.polyline(ll, {
        color: lineColor, weight: 6, opacity: 0.85, interactive: false,
      }).addTo(layer);
    }

    function profileFromTransport(html) {
      if (!html) return "driving";
      var t = html.toLowerCase();
      if (t.indexOf("walk") !== -1 || t.indexOf("步") !== -1 || t.indexOf("走") !== -1) return "walking";
      if (t.indexOf("bike") !== -1 || t.indexOf("cycle") !== -1 || t.indexOf("骑") !== -1) return "cycling";
      return "driving";
    }

    // Draw routes from the routes array.
    (routes || []).forEach(function (rt) {
      var fromPin = lookup[rt.from_name];
      var toPin = lookup[rt.to_name];
      if (!fromPin || !toPin) return;
      var routeHtml = "";
      if (rt.transport) routeHtml += "<i>" + _escapeHtml(rt.transport) + "</i>";
      if (rt.note_md) {
        var rn = renderMarkdown(_normMd(rt.note_md));
        if (rn) routeHtml += (routeHtml ? "<br>" : "") + rn;
      }
      addRouteLine([fromPin.lat, fromPin.lng], [toPin.lat, toPin.lng], routeHtml || null, rt.transport || "");
    });

    // Backward compat: render old-format route_from_prev as virtual routes.
    (pins || []).forEach(function (pin, idx) {
      if (idx === 0 || !pin.route_from_prev) return;
      var prevPin = pins[idx - 1];
      if (!prevPin || prevPin.lat == null || prevPin.lng == null) return;
      var routeHtml = "";
      if (pin.route_from_prev.transport) routeHtml += "<i>" + _escapeHtml(pin.route_from_prev.transport) + "</i>";
      if (pin.route_from_prev.note_md) {
        var rn = renderMarkdown(_normMd(pin.route_from_prev.note_md));
        if (rn) routeHtml += (routeHtml ? "<br>" : "") + rn;
      }
      addRouteLine([prevPin.lat, prevPin.lng], [pin.lat, pin.lng], routeHtml || null, (pin.route_from_prev.transport || ""));
    });

    // Fit bounds to show all markers.
    if (latlngs.length > 0) {
      try { map.fitBounds(latlngs, { padding: [30, 30], maxZoom: 12 }); } catch (e) {}
    } else {
      map.setView([35, 105], 4);
    }
  }, [pins, routes]);

  // Theme change listener: swap tile layer.
  useEffect(function () {
    var map = mapRef.current;
    if (!map || !window.L) return;
    var L = window.L;
    var observer = new MutationObserver(function () {
      var isDark = isDarkTheme();
      var provider = getMapProvider();
      map.eachLayer(function (layer) {
        if (layer instanceof L.TileLayer) map.removeLayer(layer);
      });
      L.tileLayer(getTileUrl(isDark, provider), {
        attribution: getTileAttribution(provider),
        subdomains: provider === "amap" ? [] : "abcd",
      }).addTo(map);
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return function () { observer.disconnect(); };
  }, []);

  function isDarkTheme() {
    return (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
  }

  return window.L
    ? <div className="map-container" ref={containerRef} />
    : <div className="map-container" style={{ display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-4)", fontSize: 12 }}>
        Leaflet 未加载
      </div>;
}
