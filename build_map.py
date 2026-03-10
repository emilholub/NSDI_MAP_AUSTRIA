"""
build_map.py — Austria NSDI Folium Web Map
==========================================
Builds an interactive multi-layer map from:
  • roads.gpkg      (WFS vector — tn-ro:Road, tn-ro:ERoad)
  • waterways.gpkg  (WFS vector)
  • railways.gpkg   (converted from GML)
  • dams.gpkg       (converted from Stauanlagen.xlsx)
  • WMS tile layers: buildings, DTM, flood zones (HQ30/100/300, APSFR)

Output: austria_nsdi_map.html

Requirements:
    pip install geopandas folium
"""

import warnings
from pathlib import Path

import folium
import folium.plugins as plugins
import geopandas as gpd

warnings.filterwarnings("ignore")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATASETS = BASE_DIR / "datasets"
OUT_HTML = BASE_DIR / "index.html"

# ── WMS endpoints (from wms_layers.json) ──────────────────────────────────────
WMS_BUILDINGS = "https://data.bev.gv.at/geoserver/BEVdataDLM/wms"
WMS_DTM       = ("https://wsa.bev.gv.at/GeoServer/Interceptor/Wms/EL-ALS-DTM/"
                 "INSPIRE_KUNDEN-382e30c7-69df-4a53-9331-c44821d9916e")
WMS_FLOOD     = "https://inspire.lfrz.gv.at/000801/wms"
WMS_SOIL      = "https://inspire.lfrz.gv.at/000604/ows"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_gpkg(name: str, layer_filter: str = None) -> gpd.GeoDataFrame:
    """Load a GeoPackage with verbose diagnostics at every filtering step."""
    from shapely.ops import transform as shp_transform
    from shapely.geometry.base import BaseGeometry

    path = DATASETS / f"{name}.gpkg"
    if not path.exists():
        print(f"  [SKIP] {name}.gpkg — file not found at {path}")
        return None

    gdf = gpd.read_file(path)
    print(f"  [{name}] raw rows: {len(gdf)}  |  "
          f"columns: {list(gdf.columns)[:8]}{'…' if len(gdf.columns)>8 else ''}")

    if "_layer" in gdf.columns:
        print(f"  [{name}] _layer values: {gdf['_layer'].value_counts().to_dict()}")

    if layer_filter:
        if "_layer" not in gdf.columns:
            print(f"  [{name}] WARNING — no _layer column, cannot filter to '{layer_filter}'")
        else:
            before = len(gdf)
            gdf = gdf[gdf["_layer"] == layer_filter]
            print(f"  [{name}] after _layer='{layer_filter}': {len(gdf)} / {before} rows")

    before = len(gdf)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if len(gdf) < before:
        print(f"  [{name}] dropped {before - len(gdf)} null/empty geometries")

    if len(gdf) == 0:
        print(f"  [{name}] ✗ EMPTY after filtering — layer will not appear on map")
        return None

    # Geometry type breakdown
    geom_types = gdf.geometry.geom_type.value_counts().to_dict()
    print(f"  [{name}] geometry types: {geom_types}")

    # CRS — detect if coordinates look projected (values >> 180) even when
    # the file claims EPSG:4326 (common with CRS-less GML files)
    raw_bounds = gdf.total_bounds
    looks_projected = raw_bounds[2] > 1000 or raw_bounds[3] > 1000

    if looks_projected:
        # Try Austrian projections in order of likelihood
        AUSTRIAN_CRS = [31287, 31256, 31255, 31257, 3416, 32633]
        import pyproj
        reprojected = False
        for epsg in AUSTRIAN_CRS:
            try:
                test = gdf.set_crs(epsg=epsg, allow_override=True).to_crs(epsg=4326)
                tb = test.total_bounds
                if 8 < tb[0] < 18 and 46 < tb[1] < 50:
                    print(f"  [{name}] CRS was wrong/missing — forced EPSG:{epsg} → EPSG:4326 ✓")
                    gdf = test
                    reprojected = True
                    break
            except Exception:
                continue
        if not reprojected:
            print(f"  [{name}] ⚠ Could not auto-detect CRS — coords may be wrong")
    elif gdf.crs is None:
        print(f"  [{name}] no CRS — assuming EPSG:4326")
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        print(f"  [{name}] reprojecting {gdf.crs} → EPSG:4326")
        gdf = gdf.to_crs(epsg=4326)

    # Strip Z coordinates — GML files often carry elevation values that break
    # GeoJSON serialisation in some Leaflet/browser combinations
    has_z = gdf.geometry.apply(lambda g: g.has_z if isinstance(g, BaseGeometry) else False).any()
    if has_z:
        print(f"  [{name}] stripping Z coordinates")
        gdf["geometry"] = gdf["geometry"].apply(
            lambda g: shp_transform(lambda x, y, *_: (x, y), g) if g is not None else g
        )

    # Bounds sanity check
    bounds = gdf.total_bounds  # [minx, miny, maxx, maxy]
    print(f"  [{name}] bounds: lon {bounds[0]:.3f}→{bounds[2]:.3f}  "
          f"lat {bounds[1]:.3f}→{bounds[3]:.3f}")
    if not (7 < bounds[0] < 18 and 46 < bounds[1] < 50):
        print(f"  [{name}] ⚠ WARNING — bounds look wrong for Austria, check CRS")

    print(f"  [{name}] ✓ ready  ({len(gdf)} features)")
    return gdf


def gdf_to_layer(
    gdf,
    name,
    color,
    weight=1.5,
    fill_color=None,
    fill_opacity=0.4,
    tooltip_cols=None,
    show=True,
):
    """Convert a GeoDataFrame to a styled folium FeatureGroup."""
    fg = folium.FeatureGroup(name=name, show=show)
    style = {
        "color": color,
        "weight": weight,
        "fillColor": fill_color or color,
        "fillOpacity": fill_opacity if fill_color else 0,
        "opacity": 0.85,
    }
    if tooltip_cols:
        avail = [c for c in tooltip_cols if c in gdf.columns]
        tooltip = folium.GeoJsonTooltip(fields=avail) if avail else None
    else:
        tooltip = None

    folium.GeoJson(
        gdf.to_json(),          # more robust than __geo_interface__ for GML data
        style_function=lambda _: style,
        tooltip=tooltip,
    ).add_to(fg)
    return fg


def add_wms(m, name, url, layers, show=False, attribution="", opacity=1.0, min_zoom=0, extra_params=None):
    """Add a WMS tile layer to the map."""
    # Append any extra WMS params (e.g. SLD_BODY) directly to the URL
    wms_url = url
    if extra_params:
        import urllib.parse
        sep = "&" if "?" in url else "?"
        wms_url = url + sep + urllib.parse.urlencode(extra_params)
    folium.raster_layers.WmsTileLayer(
        url=wms_url,
        name=name,
        layers=layers,
        fmt="image/png",
        transparent=True,
        version="1.3.0",
        attr=attribution,
        show=show,
        opacity=opacity,
        min_zoom=min_zoom,
    ).add_to(m)


def dam_popup(row) -> str:
    """Build an HTML popup for a dam feature."""
    fields = {
        "Name":                      row.get("Name", "—"),
        "Art der Talsperre":         row.get("Art der Talsperre", "—"),
        "Talsperrenhöhe [m]":        row.get("Talsperrenhöhe [m]", "—"),
        "Staurauminhalt [1000m³]":   row.get("Gesamtstaurauminhalt [1000m³]", "—"),
        "inst. Leistung [MW]":       row.get("installierte elektrische Leistung [MW]", "—"),
        "RAV [GWh/a]":               row.get("Regelarbeitsvermögen (RAV) - erzeugte Leistung [GWh/a]", "—"),
        "Gewässer":                  row.get("Gewässer", "—"),
        "Bauende":                   row.get("Bauende", "—"),
    }
    rows_html = "".join(
        f"<tr><td style='padding:2px 8px;color:#555'><b>{k}</b></td>"
        f"<td style='padding:2px 8px'>{v if str(v) != 'nan' else '—'}</td></tr>"
        for k, v in fields.items()
    )
    return (
        "<table style='font-size:12px;font-family:sans-serif;"
        f"border-collapse:collapse'>{rows_html}</table>"
    )


# ── Build map ─────────────────────────────────────────────────────────────────

def build_map():
    print("\n=== Building Austria NSDI Map ===\n")

    # Innsbruck — Inn riverbank at the Alte Innbrücke
    m = folium.Map(
        location=[47.2682, 11.3933],
        zoom_start=15,
        max_zoom=19,
        tiles=None,
    )

    # ── Base tile layers ──────────────────────────────────────────────────────
    # Esri Light Gray Canvas — land/water only, no roads, no labels
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="© Esri, HERE, Garmin",
        name="Light Gray (no roads)",
        show=True,
    ).add_to(m)

    # CartoDB no-labels — subtle grey, no roads
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png",
        attr="© OpenStreetMap contributors © CARTO",
        name="CartoDB (no labels)",
        show=False,
    ).add_to(m)

    # Admin boundaries only — pure white + Stamen Toner Lines (coastlines + borders only)
    # White canvas + admin/border lines only (Stamen Toner Lines)
    folium.TileLayer(
        tiles="https://tiles.stadiamaps.com/tiles/stamen_toner_lines/{z}/{x}/{y}{r}.png",
        attr="© Stadia Maps © Stamen Design © OpenStreetMap",
        name="Admin Boundaries Only",
        show=False,
    ).add_to(m)

    # ── WMS layers ────────────────────────────────────────────────────────────
    print("Adding WMS layers …")

    add_wms(m, "DTM – BEV ALS 2023",
            WMS_DTM, "EL_ALS_DTM",
            attribution="© BEV",
            show=False)

    add_wms(m, "Buildings & Structures (BEV DLM)",
            WMS_BUILDINGS, "BWK_8100_BAUWERK_F",
            attribution="© BEV DLM",
            min_zoom=14,
            show=True)

    add_wms(m, "Roads (BEV DLM)",
            WMS_BUILDINGS, "VER_1100_STRASSE_L",
            attribution="© BEV DLM",
            min_zoom=10,
            show=True)

    add_wms(m, "Railways (BEV DLM)",
            WMS_BUILDINGS, "VER_1300_BAHN_L",
            attribution="© BEV DLM",
            min_zoom=10,
            show=True)

    add_wms(m, "Power Lines (BEV DLM)",
            WMS_BUILDINGS, "BAU_2700_STROMLEITUNG_L",
            attribution="© BEV DLM",
            min_zoom=12,
            show=True)

    # HQ30: server renders grey; CSS filter shifts to light blue client-side
    add_wms(m, "Flood Inundation – HQ30",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ30",
            attribution="© LFRZ / HWRL",
            show=True,
            opacity=0.6)
    add_wms(m, "Flood Inundation – HQ100",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ100",
            attribution="© LFRZ / HWRL",
            show=False)

    add_wms(m, "Flood Inundation – HQ300",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ300",
            attribution="© LFRZ / HWRL",
            show=False)

    add_wms(m, "Significant Flood Risk Areas (APSFR)",
            WMS_FLOOD, "APSFR",
            attribution="© LFRZ / HWRL",
            show=False)

    add_wms(m, "Danger Zones – Red (Gefahrenzonenplan)",
            WMS_FLOOD, "Rote Gefahrenzonen aus der Gefahrenzonenplanung",
            attribution="© LFRZ",
            show=False)

    add_wms(m, "Danger Zones – Yellow (Gefahrenzonenplan)",
            WMS_FLOOD, "Gelbe Gefahrenzonen aus der Gefahrenzonenplanung",
            attribution="© LFRZ",
            show=False)

    # ── Soil WMS layers (BFW Agricultural Soil Map) ───────────────────────────
    add_wms(m, "Soil – WRB Reference Soil Group",
            WMS_SOIL,
            "Agricultural Soil Map of Austria - Soil Reference Group Code from the World Reference Base (WRB Lev1)",
            attribution="© BFW",
            opacity=0.45,
            show=False)

    # ── Vector layers ─────────────────────────────────────────────────────────
    print("Loading vector layers …")

    # tn-ro:Road is an abstract INSPIRE feature with no direct geometry.
    # Scan all _layer values and keep only those whose features are Lines.
    roads_all = load_gpkg("roads")
    if roads_all is not None:
        from shapely.geometry import LineString, MultiLineString
        line_mask = roads_all.geometry.apply(
            lambda g: isinstance(g, (LineString, MultiLineString))
        )
        roads = roads_all[line_mask & (roads_all["_layer"] != "tn-ro:ERoad")]
        if len(roads):
            print(f"  Roads:      {len(roads)} features "
                  f"(layers: {roads['_layer'].unique().tolist()})")
            gdf_to_layer(
                roads, "Roads (NSDI)",
                color="#b07840", weight=1.2,
                tooltip_cols=["localId", "beginLifespanVersion", "_layer"],
                show=True,
            ).add_to(m)
        else:
            print("  Roads: WFS source has no line geometry — "
                  "consider adding a BEV DLM roads WMS layer instead")

        eroads = roads_all[
            (roads_all["_layer"] == "tn-ro:ERoad") & line_mask
        ]
        if len(eroads):
            print(f"  E-Roads:    {len(eroads)} features")
            gdf_to_layer(
                eroads, "E-Roads (European network)",
                color="#c0392b", weight=2.5,
                tooltip_cols=["europeanRouteNumber", "localId"],
                show=False,
            ).add_to(m)

    waterways = load_gpkg("waterways")
    if waterways is not None:
        print(f"  Waterways:  {len(waterways)} features")
        gdf_to_layer(
            waterways, "Waterways (NSDI)",
            color="#2980b9", weight=1.5,
            tooltip_cols=["localId", "geographicalName", "_layer"],
            show=True,
        ).add_to(m)

    railways = load_gpkg("railways")
    if railways is not None:
        print(f"  Railways:   {len(railways)} features")
        # Sanitise column names — GML often produces cols like "tn-ra:foo"
        # which break GeoJSON tooltip lookup; replace non-alphanumeric with _
        import re
        railways.columns = [
            re.sub(r"[^a-zA-Z0-9_]", "_", c) for c in railways.columns
        ]
        gdf_to_layer(
            railways, "Railways – GML vector (ÖBB)",
            color="#8e44ad", weight=1.8,
            tooltip_cols=["gml_id", "name", "_layer"],
            show=False,   # DLM WMS railway is the primary; keep this for tooltip queries
        ).add_to(m)

    dams = load_gpkg("dams")
    if dams is not None:
        print(f"  Dams:       {len(dams)} features")
        fg_dams = folium.FeatureGroup(name="Dams / Stauanlagen", show=True)
        for _, row in dams.iterrows():
            lat, lon = row.geometry.y, row.geometry.x
            # Scale dot size by reservoir volume
            try:
                radius = max(4, min(14, float(row["Gesamtstaurauminhalt [1000m³]"]) / 5000 * 10 + 4))
            except (TypeError, ValueError):
                radius = 5
            folium.CircleMarker(
                location=[lat, lon],
                radius=radius,
                color="#1a6b8a",
                fill=True,
                fill_color="#2196F3",
                fill_opacity=0.75,
                weight=1.5,
                popup=folium.Popup(dam_popup(row), max_width=320),
                tooltip=str(row.get("Name", "Dam")),
            ).add_to(fg_dams)
        fg_dams.add_to(m)

    # ── Plugins & controls ────────────────────────────────────────────────────
    folium.LayerControl(collapsed=False, position="topright").add_to(m)
    plugins.Fullscreen().add_to(m)
    plugins.MeasureControl(position="bottomleft", primary_length_unit="kilometers").add_to(m)
    plugins.MousePosition(position="bottomright").add_to(m)
    plugins.MiniMap(toggle_display=True).add_to(m)
    # ── Scale bar + zoom-based layer visibility ──────────────────────────────
    # CSS filter: shifts server-rendered grey HQ30 tiles to light blue
    m.get_root().script.add_child(folium.Element("""
    document.addEventListener('DOMContentLoaded', function() {
      function applyHQ30Filter() {
        var map = Object.values(window).find(function(v) {
          return v && v._leaflet_id !== undefined && typeof v.addControl === 'function';
        });
        if (!map) return;
        map.eachLayer(function(layer) {
          if (layer.options && layer.options.layers &&
              layer.options.layers.indexOf('HQ30') !== -1 &&
              layer._container) {
            layer._container.style.filter =
              'hue-rotate(195deg) saturate(3) brightness(1.6)';
          }
        });
      }
      setTimeout(applyHQ30Filter, 800);
      document.addEventListener('tileload', applyHQ30Filter);
    });
    """))

    m.get_root().script.add_child(folium.Element("""
    document.addEventListener('DOMContentLoaded', function() {
      var map = Object.values(window).find(function(v){
        return v && v._leaflet_id !== undefined && typeof v.addControl === 'function';
      });
      if (!map) return;

      // Metric scale bar
      L.control.scale({imperial: false, position: 'bottomleft'}).addTo(map);

      var MIN_ZOOM = 8;

      // After a short delay (layers finish rendering), snapshot each GeoJSON
      // child layer's original style so we can restore it exactly on zoom-in.
      setTimeout(function() {
        map.eachLayer(function(fg) {
          if (!fg._layers) return;                        // not a FeatureGroup
          Object.values(fg._layers).forEach(function(child) {
            if (child.options) {
              child._origOpacity     = child.options.opacity     !== undefined ? child.options.opacity     : 0.85;
              child._origFillOpacity = child.options.fillOpacity !== undefined ? child.options.fillOpacity : 0;
              child._origWeight      = child.options.weight      !== undefined ? child.options.weight      : 1.5;
            }
          });
        });
      }, 500);

      function gateZoom() {
        var z = map.getZoom();
        map.eachLayer(function(fg) {
          if (!fg._layers || !fg.setStyle) return;        // skip non-vector layers
          if (z < MIN_ZOOM) {
            fg.setStyle({opacity: 0, fillOpacity: 0, weight: 0});
          } else {
            // Restore each child to its individual original style
            Object.values(fg._layers).forEach(function(child) {
              if (child.setStyle && child._origOpacity !== undefined) {
                child.setStyle({
                  opacity:     child._origOpacity,
                  fillOpacity: child._origFillOpacity,
                  weight:      child._origWeight
                });
              }
            });
          }
        });
      }

      map.on('zoomend', gateZoom);
    });
    """))

    # ── Floating soil legend (fetched live from WMS GetLegendGraphic) ────────
    legend_url = (
        "https://inspire.lfrz.gv.at/000604/ows?service=WMS&version=1.3.0&request=GetLegendGraphic&format=image%2Fpng&width=20&height=20&layer=Agricultural%20Soil%20Map%20of%20Austria%20-%20Soil%20Reference%20Group%20Code%20from%20the%20World%20Reference%20Base%20%28WRB%20Lev1%29&style=BE_WRB_Lev1"
    )
    legend_html = (
        '<div id="soil-legend" style="'
        'position:fixed;bottom:60px;left:10px;z-index:1000;'
        'background:rgba(255,255,255,0.92);padding:8px 10px;'
        'border-radius:6px;border:1px solid #ccc;'
        'box-shadow:2px 2px 6px rgba(0,0,0,0.15);'
        'font-size:11px;font-family:sans-serif;max-width:200px;">'
        '<b style="display:block;margin-bottom:4px">WRB Reference Soil Group</b>'
        f'<img src="{legend_url}" style="max-width:180px" alt="Soil legend"'
        ' onerror="this.parentElement.innerHTML=\'<i>Legend unavailable</i>\'">'
        '</div>'
    )
    m.get_root().html.add_child(folium.Element(legend_html))

    # ── Save ──────────────────────────────────────────────────────────────────
    m.save(str(OUT_HTML))
    size_kb = OUT_HTML.stat().st_size / 1024
    print(f"\n✓ Saved → {OUT_HTML.name}  ({size_kb:.1f} KB)")
    print("  Open in a browser to explore.\n")


if __name__ == "__main__":
    build_map()
