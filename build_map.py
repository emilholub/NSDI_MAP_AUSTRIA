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
OUT_HTML = BASE_DIR / "austria_nsdi_map.html"

# ── WMS endpoints (from wms_layers.json) ──────────────────────────────────────
WMS_BUILDINGS = "https://data.bev.gv.at/geoserver/BEVdataDLM/wms"
WMS_DTM       = ("https://wsa.bev.gv.at/GeoServer/Interceptor/Wms/EL-ALS-DTM/"
                 "INSPIRE_KUNDEN-382e30c7-69df-4a53-9331-c44821d9916e")
WMS_FLOOD     = "https://inspire.lfrz.gv.at/000801/wms"
WMS_SOIL      = "https://inspire.lfrz.gv.at/000604/ows"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_gpkg(name: str, layer_filter: str = None) -> gpd.GeoDataFrame:
    """Load a GeoPackage, optionally filtering to one _layer value."""
    path = DATASETS / f"{name}.gpkg"
    if not path.exists():
        print(f"  [SKIP] {name}.gpkg not found")
        return None
    gdf = gpd.read_file(path)
    if layer_filter and "_layer" in gdf.columns:
        gdf = gdf[gdf["_layer"] == layer_filter]
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    # Strip Z coordinates — GML files often carry elevation values that break
    # GeoJSON serialisation in some Leaflet/browser combinations
    from shapely.ops import transform
    gdf["geometry"] = gdf["geometry"].apply(
        lambda geom: transform(lambda x, y, *_: (x, y), geom) if geom is not None else geom
    )
    return gdf if len(gdf) > 0 else None


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
        gdf.__geo_interface__,
        style_function=lambda _: style,
        tooltip=tooltip,
    ).add_to(fg)
    return fg


def add_wms(m, name, url, layers, show=False, attribution="", opacity=1.0, min_zoom=0):
    """Add a WMS tile layer to the map."""
    folium.raster_layers.WmsTileLayer(
        url=url,
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

    m = folium.Map(
        location=[47.5, 13.5],
        zoom_start=7,
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

    # Fallback with subtle labels if needed
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png",
        attr="© OpenStreetMap contributors © CARTO",
        name="CartoDB (no labels)",
        show=False,
    ).add_to(m)

    # ── WMS layers ────────────────────────────────────────────────────────────
    print("Adding WMS layers …")

    add_wms(m, "DTM – BEV ALS 2023",
            WMS_DTM, "EL_ALS_DTM",
            attribution="© BEV")

    add_wms(m, "Buildings & Structures (BEV DLM)",
            WMS_BUILDINGS, "BWK_8100_BAUWERK_F",
            attribution="© BEV DLM",
            min_zoom=14)

    add_wms(m, "Power Lines (BEV DLM)",
            WMS_BUILDINGS, "BAU_2700_STROMLEITUNG_L",
            attribution="© BEV DLM",
            min_zoom=12)

    add_wms(m, "Flood Inundation – HQ30",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ30",
            attribution="© LFRZ / HWRL")

    add_wms(m, "Flood Inundation – HQ100",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ100",
            attribution="© LFRZ / HWRL")

    add_wms(m, "Flood Inundation – HQ300",
            WMS_FLOOD, "Hochwasserueberflutungsflaechen HQ300",
            attribution="© LFRZ / HWRL")

    add_wms(m, "Significant Flood Risk Areas (APSFR)",
            WMS_FLOOD, "APSFR",
            attribution="© LFRZ / HWRL")

    add_wms(m, "Danger Zones – Red (Gefahrenzonenplan)",
            WMS_FLOOD, "Rote Gefahrenzonen aus der Gefahrenzonenplanung",
            attribution="© LFRZ")

    add_wms(m, "Danger Zones – Yellow (Gefahrenzonenplan)",
            WMS_FLOOD, "Gelbe Gefahrenzonen aus der Gefahrenzonenplanung",
            attribution="© LFRZ")

    # ── Soil WMS layers (BFW Agricultural Soil Map) ───────────────────────────
    add_wms(m, "Soil – WRB Reference Soil Group",
            WMS_SOIL,
            "Agricultural Soil Map of Austria - Soil Reference Group Code from the World Reference Base (WRB Lev1)",
            attribution="© BFW",
            opacity=0.45)

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
            print("  Roads: no line geometry found — check roads.gpkg layer names")

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
        gdf_to_layer(
            railways, "Railways (ÖBB)",
            color="#8e44ad", weight=1.5,
            tooltip_cols=["gml_id", "name", "_layer"],
            show=True,
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
