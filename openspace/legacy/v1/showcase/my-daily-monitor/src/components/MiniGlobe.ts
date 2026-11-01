/**
 * MiniGlobe — a lightweight CSS/SVG animated globe.
 * Rendered as a self-contained DOM element that can be inserted into any panel.
 * Shows animated pulsing dots at random geo locations to indicate live activity.
 */

const GLOBE_SVG = `<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" class="mini-globe">
  <!-- Ocean -->
  <circle cx="50" cy="50" r="48" fill="#041a14" stroke="rgba(68,255,136,0.25)" stroke-width="1"/>
  <!-- Atmosphere glow -->
  <circle cx="50" cy="50" r="48" fill="none" stroke="rgba(68,255,136,0.08)" stroke-width="6"/>
  <!-- Grid lines (meridians + parallels) -->
  <ellipse cx="50" cy="50" rx="48" ry="20" fill="none" stroke="rgba(68,255,136,0.12)" stroke-width="0.5"/>
  <ellipse cx="50" cy="50" rx="48" ry="35" fill="none" stroke="rgba(68,255,136,0.08)" stroke-width="0.5"/>
  <ellipse cx="50" cy="50" rx="20" ry="48" fill="none" stroke="rgba(68,255,136,0.12)" stroke-width="0.5"/>
  <ellipse cx="50" cy="50" rx="35" ry="48" fill="none" stroke="rgba(68,255,136,0.08)" stroke-width="0.5"/>
  <line x1="2" y1="50" x2="98" y2="50" stroke="rgba(68,255,136,0.1)" stroke-width="0.5"/>
  <line x1="50" y1="2" x2="50" y2="98" stroke="rgba(68,255,136,0.1)" stroke-width="0.5"/>
  <!-- Simplified land masses -->
  <!-- North America -->
  <path d="M22,25 Q28,22 32,25 Q35,28 33,32 Q30,36 26,38 Q22,36 20,32 Q19,28 22,25Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
  <!-- South America -->
  <path d="M30,50 Q34,48 36,52 Q37,58 35,64 Q32,68 29,65 Q27,60 28,55 Q29,52 30,50Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
  <!-- Europe -->
  <path d="M48,22 Q52,20 55,23 Q56,26 54,28 Q51,30 48,28 Q47,25 48,22Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
  <!-- Africa -->
  <path d="M48,35 Q52,33 55,36 Q57,42 56,50 Q54,56 50,58 Q46,55 45,48 Q45,40 48,35Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
  <!-- Asia -->
  <path d="M58,20 Q66,18 74,22 Q78,28 76,34 Q72,38 66,36 Q60,34 58,28 Q57,24 58,20Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
  <!-- Australia -->
  <path d="M72,52 Q76,50 78,53 Q79,56 77,58 Q74,59 72,57 Q71,55 72,52Z" fill="rgba(68,255,136,0.2)" stroke="rgba(68,255,136,0.35)" stroke-width="0.5"/>
</svg>`;

// Predefined pulse dot positions (approximate screen coords within 100x100 viewbox)
const PULSE_POSITIONS = [
  { x: 28, y: 30 },   // North America
  { x: 33, y: 58 },   // South America
  { x: 52, y: 25 },   // Europe
  { x: 50, y: 44 },   // Africa
  { x: 68, y: 28 },   // Asia
  { x: 75, y: 55 },   // Australia
  { x: 62, y: 22 },   // Russia
  { x: 40, y: 32 },   // Atlantic
];

export function createMiniGlobe(): HTMLElement {
  const wrapper = document.createElement('div');
  wrapper.className = 'mini-globe-wrapper';
  wrapper.innerHTML = GLOBE_SVG;

  // Add pulsing activity dots
  const activeDots = 3 + Math.floor(Math.random() * 3);
  const chosen = [...PULSE_POSITIONS].sort(() => Math.random() - 0.5).slice(0, activeDots);

  for (const pos of chosen) {
    const dot = document.createElement('div');
    dot.className = 'mini-globe-pulse';
    dot.style.left = `${pos.x}%`;
    dot.style.top = `${pos.y}%`;
    dot.style.animationDelay = `${Math.random() * 2}s`;
    wrapper.appendChild(dot);
  }

  return wrapper;
}

