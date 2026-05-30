// Map view for the right sidebar — Leaflet-based interactive map.
// Displays pins and routes placed by the agent via pin_location / connect_pins.
const { useState, useEffect, useRef } = React;
const renderMarkdown = window.renderMarkdown || function (t) { return String(t || ""); };
var _escapeHtml = function (s) { return String(s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); };
// Normalize literal \n in JSON strings to actual newlines for markdown rendering.
var _normMd = function (s) { return String(s || "").replace(/\\n/g, "\n"); };

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

      var isDark = (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
      var tileUrl = isDark
        ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
      var attribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, &copy; <a href="https://carto.com/">CARTO</a>';

      var map = L.map(el, {
        zoomControl: false,
        attributionControl: true,
      }).setView([35, 105], 4);

      L.tileLayer(tileUrl, { attribution: attribution, subdomains: "abcd" }).addTo(map);
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

  // Build a lookup map: pin name → {lat, lng}.
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

    var lookup = pinLookup();
    var latlngs = [];

    // Draw markers for each pin.
    (pins || []).forEach(function (pin) {
      if (pin.lat == null || pin.lng == null) return;
      var ll = [pin.lat, pin.lng];
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

    // Helper: draw a route line with wide invisible hit area around it.
    var lineColor = isDarkTheme() ? "#6ea8fe" : "#0d6efd";
    function addRouteLine(fromLl, toLl, popupHtml) {
      // Invisible wide hit area (weight ~20px around the line).
      var hit = L.polyline([fromLl, toLl], {
        weight: 20, opacity: 0, interactive: true,
      });
      if (popupHtml) hit.bindPopup(popupHtml, { maxWidth: 250 });
      layer.addLayer(hit);
      // Visible line on top (non-interactive — clicks pass through to hit).
      L.polyline([fromLl, toLl], {
        color: lineColor, weight: 6, opacity: 0.7, dashArray: "8, 6",
        interactive: false,
      }).addTo(layer);
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
      addRouteLine([fromPin.lat, fromPin.lng], [toPin.lat, toPin.lng], routeHtml || null);
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
      addRouteLine([prevPin.lat, prevPin.lng], [pin.lat, pin.lng], routeHtml || null);
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
      var isDark = (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
      map.eachLayer(function (layer) {
        if (layer instanceof L.TileLayer) map.removeLayer(layer);
      });
      var tileUrl = isDark
        ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
      L.tileLayer(tileUrl, {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: "abcd",
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
