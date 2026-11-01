/**
 * WeatherPanel — weather + local clock + AoE clock.
 * Auto-detects location via browser Geolocation API.
 * Uses Open-Meteo (free, no API key) + Nominatim for reverse geocoding.
 */
import { Panel } from './Panel';

const OPEN_METEO_API = 'https://api.open-meteo.com/v1/forecast';
const NOMINATIM_API = 'https://nominatim.openstreetmap.org/reverse';

interface WeatherData {
  temperature: number;
  feelsLike: number;
  humidity: number;
  windSpeed: number;
  weatherCode: number;
  isDay: boolean;
  timezone: string;
  daily: Array<{ date: string; tempMax: number; tempMin: number; code: number }>;
}

function weatherIcon(code: number, isDay: boolean): string {
  if (code === 0) return isDay ? '☀️' : '🌙';
  if (code <= 3) return isDay ? '⛅' : '☁️';
  if (code <= 48) return '🌫️';
  if (code <= 57) return '🌧️';
  if (code <= 67) return '🌧️';
  if (code <= 77) return '❄️';
  if (code <= 82) return '🌧️';
  if (code <= 86) return '❄️';
  if (code <= 99) return '⛈️';
  return '🌡️';
}

function weatherDesc(code: number): string {
  if (code === 0) return 'Clear';
  if (code <= 3) return 'Partly cloudy';
  if (code <= 48) return 'Foggy';
  if (code <= 57) return 'Drizzle';
  if (code <= 67) return 'Rain';
  if (code <= 77) return 'Snow';
  if (code <= 82) return 'Rain showers';
  if (code <= 86) return 'Snow showers';
  if (code <= 99) return 'Thunderstorm';
  return 'Unknown';
}

export class WeatherPanel extends Panel {
  private lat = 0;
  private lon = 0;
  private cityName = '';
  private locationReady = false;
  private clockTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'weather', title: 'Weather & Time', showCount: false });
    this.showLoading('Detecting location...');
    this.detectLocation();
    // Update clocks every second
    this.clockTimer = setInterval(() => this.updateClocks(), 1000);
  }

  private detectLocation(): void {
    if (!('geolocation' in navigator)) {
      this.showError('Geolocation not available in this browser', () => this.detectLocation());
      return;
    }

    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        this.lat = pos.coords.latitude;
        this.lon = pos.coords.longitude;
        this.locationReady = true;

        // Reverse geocode to get city name
        try {
          const resp = await fetch(`${NOMINATIM_API}?lat=${this.lat}&lon=${this.lon}&format=json&zoom=10`, {
            headers: { 'User-Agent': 'MyDailyMonitor/1.0' },
          });
          if (resp.ok) {
            const data = await resp.json() as any;
            this.cityName = data.address?.city || data.address?.town || data.address?.county || data.address?.state || '';
          }
        } catch { /* city name optional */ }

        this.refresh();
      },
      (err) => {
        // Fallback: try IP-based geolocation
        this.fallbackIPLocation();
      },
      { timeout: 8000, enableHighAccuracy: false }
    );
  }

  private async fallbackIPLocation(): Promise<void> {
    try {
      const resp = await fetch('https://ipapi.co/json/');
      if (resp.ok) {
        const data = await resp.json() as any;
        this.lat = data.latitude;
        this.lon = data.longitude;
        this.cityName = data.city || '';
        this.locationReady = true;
        this.refresh();
      } else {
        this.showError('Could not detect location. Allow location access or check network.', () => this.detectLocation());
      }
    } catch {
      this.showError('Could not detect location.', () => this.detectLocation());
    }
  }

  async refresh(): Promise<void> {
    if (!this.locationReady) return;
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const url = `${OPEN_METEO_API}?latitude=${this.lat}&longitude=${this.lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=5`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json() as any;

      const data: WeatherData = {
        temperature: json.current.temperature_2m,
        feelsLike: json.current.apparent_temperature,
        humidity: json.current.relative_humidity_2m,
        windSpeed: json.current.wind_speed_10m,
        weatherCode: json.current.weather_code,
        isDay: json.current.is_day === 1,
        timezone: json.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone,
        daily: (json.daily?.time || []).map((d: string, i: number) => ({
          date: d,
          tempMax: json.daily.temperature_2m_max[i],
          tempMin: json.daily.temperature_2m_min[i],
          code: json.daily.weather_code[i],
        })),
      };

      this.render(data);
      this.setDataBadge('live');
    } catch {
      this.showError('Weather unavailable', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private updateClocks(): void {
    const localEl = this.content.querySelector('#localClock');
    const aoeEl = this.content.querySelector('#aoeClock');
    if (!localEl || !aoeEl) return;

    const now = new Date();
    localEl.textContent = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

    // AoE = UTC-12 (Anywhere on Earth)
    aoeEl.textContent = now.toLocaleTimeString('en-US', { timeZone: 'Etc/GMT+12', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  private render(w: WeatherData): void {
    const icon = weatherIcon(w.weatherCode, w.isDay);
    const desc = weatherDesc(w.weatherCode);
    const now = new Date();
    const localTime = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const aoeTime = now.toLocaleTimeString('en-US', { timeZone: 'Etc/GMT+12', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const localDate = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    const aoeDate = now.toLocaleDateString('en-US', { timeZone: 'Etc/GMT+12', weekday: 'short', month: 'short', day: 'numeric' });
    const tzAbbr = w.timezone.replace(/_/g, ' ').split('/').pop() || '';

    const forecastHtml = w.daily.slice(1).map(d => {
      const dayName = new Date(d.date + 'T12:00:00').toLocaleDateString('en', { weekday: 'short' });
      return `
        <div class="weather-forecast-day">
          <span class="weather-forecast-name">${dayName}</span>
          <span class="weather-forecast-icon">${weatherIcon(d.code, true)}</span>
          <span class="weather-forecast-temps">
            <span class="weather-temp-hi">${Math.round(d.tempMax)}°</span>
            <span class="weather-temp-lo">${Math.round(d.tempMin)}°</span>
          </span>
        </div>`;
    }).join('');

    this.setContent(`
      <div class="weather-container">
        <div class="weather-clocks">
          <div class="weather-clock-item">
            <span class="weather-clock-label">${this.cityName || tzAbbr || 'Local'}</span>
            <span class="weather-clock-time" id="localClock">${localTime}</span>
            <span class="weather-clock-date">${localDate}</span>
          </div>
          <div class="weather-clock-item">
            <span class="weather-clock-label">AoE (UTC-12)</span>
            <span class="weather-clock-time" id="aoeClock">${aoeTime}</span>
            <span class="weather-clock-date">${aoeDate}</span>
          </div>
        </div>
        <div class="weather-current">
          <div class="weather-main">
            <span class="weather-icon-lg">${icon}</span>
            <div>
              <span class="weather-temp-lg">${Math.round(w.temperature)}°C</span>
              <div class="weather-desc">${desc}${this.cityName ? ` · ${this.cityName}` : ''}</div>
            </div>
          </div>
          <div class="weather-details">
            <span>Feels ${Math.round(w.feelsLike)}°</span>
            <span>💧 ${w.humidity}%</span>
            <span>💨 ${Math.round(w.windSpeed)} km/h</span>
          </div>
        </div>
        <div class="weather-forecast">${forecastHtml}</div>
      </div>
    `);
  }

  public destroy(): void {
    if (this.clockTimer) clearInterval(this.clockTimer);
    super.destroy();
  }
}
