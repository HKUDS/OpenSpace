/**
 * MapPanel — interactive world map showing real-time data overlays.
 * 
 * Data layers:
 *  🟢 Your location
 *  🔴 Critical/high-threat news alerts (pulsing)
 *  🔵 News sources
 *  🟡 Server probes (up=green, down=red)
 *  📅 Calendar event locations
 *
 * Includes a legend overlay explaining all marker types.
 */
import { Panel } from './Panel';

// ---- Map tile styles ----
function buildMapStyle(tileUrls: string[], name: string) {
  return {
    version: 8 as const,
    name,
    sources: {
      osm: { type: 'raster' as const, tiles: tileUrls, tileSize: 256, attribution: '&copy; OSM &copy; CARTO' },
    },
    layers: [
      { id: 'osm-tiles', type: 'raster' as const, source: 'osm', minzoom: 0, maxzoom: 19 },
    ],
  };
}

const DARK_TILES = [
  'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
  'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
  'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
];
const LIGHT_TILES = [
  'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
  'https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
  'https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
];
// Voyager — warm, friendly style (matches happy.worldmonitor)
const VOYAGER_TILES = [
  'https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png',
  'https://b.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png',
  'https://c.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png',
];

function getMapStyle() {
  const variant = document.documentElement.dataset.variant;
  const theme = document.documentElement.dataset.theme;
  if (variant === 'happy') {
    return theme === 'dark'
      ? buildMapStyle(DARK_TILES, 'dark-osm')
      : buildMapStyle(VOYAGER_TILES, 'voyager');
  }
  return theme === 'light'
    ? buildMapStyle(LIGHT_TILES, 'light-osm')
    : buildMapStyle(DARK_TILES, 'dark-osm');
}

function isHappyLight(): boolean {
  return document.documentElement.dataset.variant === 'happy'
    && document.documentElement.dataset.theme !== 'dark';
}

export interface MapMarker {
  id: string;
  lat: number;
  lng: number;
  title: string;
  type: 'news' | 'schedule' | 'alert' | 'activity' | 'server-up' | 'server-down';
  color?: string;
  description?: string;
  url?: string;
  pulse?: boolean;
}

export class MapPanel extends Panel {
  private mapContainer: HTMLElement;
  private map: any = null;
  private markers: MapMarker[] = [];
  private markerEls: any[] = [];
  private mapReady = false;

  private legendEl: HTMLElement;

  constructor() {
    super({ id: 'map', title: 'Global Map', className: 'panel-wide', showCount: true });
    this.content.style.padding = '0';
    this.content.style.overflow = 'hidden';
    this.content.style.position = 'relative';

    this.mapContainer = document.createElement('div');
    this.mapContainer.style.cssText = 'width:100%;height:100%;min-height:200px;';
    this.content.innerHTML = '';
    this.content.appendChild(this.mapContainer);

    // Legend overlay
    this.legendEl = document.createElement('div');
    this.legendEl.className = 'map-legend';
    this.updateLegendColors();
    this.content.appendChild(this.legendEl);

    // Listen for theme changes to switch map style
    window.addEventListener('mdm-theme-changed', () => this.switchMapStyle());

    this.initMap();
  }

  /** Update legend dot colors based on theme */
  private updateLegendColors(): void {
    const happy = isHappyLight();
    const green = happy ? '#6B8F5E' : '#44ff88';
    const red = happy ? '#C48B9F' : '#ff4444';
    const blue = happy ? '#7BA5C4' : '#3b82f6';
    this.legendEl.innerHTML = `
      <div class="map-legend-title">LEGEND</div>
      <div class="map-legend-item"><span class="map-legend-dot" style="background:${green}"></span> Your Location</div>
      <div class="map-legend-item"><span class="map-legend-dot map-dot-pulse" style="background:${red}"></span> Alert News</div>
      <div class="map-legend-item"><span class="map-legend-dot" style="background:${blue}"></span> News Source</div>
      <div class="map-legend-item"><span class="map-legend-dot" style="background:${green}"></span> Server UP</div>
      <div class="map-legend-item"><span class="map-legend-dot" style="background:${red}"></span> Server DOWN</div>
    `;
  }

  private async initMap(): Promise<void> {
    const cssHref = 'https://unpkg.com/maplibre-gl@5.1.0/dist/maplibre-gl.css';
    if (!document.querySelector(`link[href*="maplibre-gl"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = cssHref;
      document.head.appendChild(link);
    }

    try {
      const maplibregl = await import('maplibre-gl');
      this.map = new maplibregl.Map({
        container: this.mapContainer,
        style: getMapStyle() as any,
        center: [20, 30],
        zoom: 1.8,
        attributionControl: false,
      });
      this.map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');
      this.map.on('load', () => {
        this.mapReady = true;
        this.setDataBadge('live');
        this.renderMarkers();
      });
    } catch (err) {
      console.warn('[MapPanel] MapLibre load failed:', err);
      this.mapContainer.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;background:var(--bg-secondary);color:var(--text-dim);font-size:11px;">Map loading failed</div>';
    }
  }

  public setMarkers(markers: MapMarker[]): void {
    this.markers = markers;
    this.setCount(markers.length);
    if (this.mapReady) this.renderMarkers();
  }

  public addMarker(marker: MapMarker): void {
    // Deduplicate by id
    this.markers = this.markers.filter(m => m.id !== marker.id);
    this.markers.push(marker);
    this.setCount(this.markers.length);
    if (this.mapReady) this.renderMarkers();
  }

  private async renderMarkers(): Promise<void> {
    for (const m of this.markerEls) { try { m.remove(); } catch {} }
    this.markerEls = [];
    if (!this.map) return;

    const maplibregl = await import('maplibre-gl');

    for (const m of this.markers) {
      const color = m.color || this.getColor(m.type);
      const size = m.type === 'alert' ? 14 : m.type === 'activity' ? 12 : 10;

      const el = document.createElement('div');
      el.style.cssText = `width:${size}px;height:${size}px;background:${color};border-radius:50%;border:1.5px solid rgba(255,255,255,0.4);cursor:pointer;box-shadow:0 0 8px ${color};transition:transform 0.15s;`;
      if (m.pulse || m.type === 'alert') {
        el.style.animation = 'map-marker-pulse 2s ease-in-out infinite';
      }
      el.title = m.title;
      el.addEventListener('mouseenter', () => { el.style.transform = 'scale(1.8)'; });
      el.addEventListener('mouseleave', () => { el.style.transform = 'scale(1)'; });

      const typeLabel = this.getTypeLabel(m.type);
      const popup = new maplibregl.Popup({ offset: 12, closeButton: false, maxWidth: '260px' })
        .setHTML(`
          <div style="font-family:var(--font-body);font-size:11px;line-height:1.4;">
            <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:${color};margin-bottom:3px;">${typeLabel}</div>
            <div style="font-weight:600;margin-bottom:4px;">${m.title}</div>
            ${m.description ? `<div style="color:#aaa;font-size:10px;">${m.description}</div>` : ''}
            ${m.url ? `<a href="${m.url}" target="_blank" style="color:#3b82f6;font-size:10px;">Open →</a>` : ''}
          </div>
        `);

      const marker = new maplibregl.Marker({ element: el })
        .setLngLat([m.lng, m.lat])
        .setPopup(popup)
        .addTo(this.map);
      this.markerEls.push(marker);
    }
  }

  private getColor(type: MapMarker['type']): string {
    const happy = isHappyLight();
    switch (type) {
      case 'news': return happy ? '#7BA5C4' : '#3b82f6';
      case 'schedule': return happy ? '#6B8F5E' : '#44ff88';
      case 'alert': return happy ? '#C4A35A' : '#ff4444';
      case 'activity': return happy ? '#6B8F5E' : '#44ff88';
      case 'server-up': return happy ? '#6B8F5E' : '#44ff88';
      case 'server-down': return happy ? '#C48B9F' : '#ff4444';
      default: return '#888';
    }
  }

  /** Switch map tiles when theme changes (dark ↔ light, default ↔ happy) */
  private switchMapStyle(): void {
    if (!this.map) return;
    this.map.setStyle(getMapStyle() as any);
    this.updateLegendColors();
    // Re-render markers after style loads
    this.map.once('style.load', () => this.renderMarkers());
  }

  private getTypeLabel(type: MapMarker['type']): string {
    switch (type) {
      case 'news': return '📰 News Source';
      case 'schedule': return '📅 Calendar Event';
      case 'alert': return '🔴 Alert';
      case 'activity': return '📍 Location';
      case 'server-up': return '🟢 Server UP';
      case 'server-down': return '🔴 Server DOWN';
      default: return 'Marker';
    }
  }

  public flyTo(lng: number, lat: number, zoom = 6): void {
    if (this.map) this.map.flyTo({ center: [lng, lat], zoom, duration: 1500 });
  }

  async refresh(): Promise<void> {}

  public destroy(): void {
    if (this.map) { this.map.remove(); this.map = null; }
    super.destroy();
  }
}
