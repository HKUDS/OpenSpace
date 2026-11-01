/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        serif: 'var(--font-serif)',
        sans:  'var(--font-sans)',
        mono:  'var(--font-mono)',
      },
      colors: {
        'bg-page':      'var(--color-bg-page)',
        'surface': 'var(--color-surface)',
        'primary': 'var(--color-primary)',
        'ink':     'var(--color-ink)',
        'muted':     'var(--color-muted)',
        'accent':  'var(--color-accent)',
        'danger':  'var(--color-danger)',
        'oxide':   'var(--color-oxide)',
        'teal': 'var(--color-teal)',
        'gold': 'var(--color-gold)',
        'red':  'var(--color-red)',
        'diff-add':      'var(--color-diff-add)',
        'diff-del':      'var(--color-diff-del)',
        'border':        'var(--color-border)',
        'border-dark':   'var(--color-border-dark)',
        'mid-gray':      'var(--color-mid-gray)',
        'blue':          'var(--color-blue)',
        'green':         'var(--color-green)',
        'paper':         'var(--color-paper)',
        'warm-gray':     'var(--color-warm-gray)',
        'sage':          'var(--color-sage)',
        'lavender':      'var(--color-lavender)',
        'sand':          'var(--color-sand)',
        'selection':     'var(--color-selection)',
      },
      backgroundImage: {
        'scanlines': 'linear-gradient(to bottom, transparent 50%, rgba(74, 59, 42, 0.03) 50%)',
        'vignette': 'radial-gradient(circle at center, transparent 60%, rgba(74, 59, 42, 0.12) 120%)',
        'noise': 'url("data:image/svg+xml,%3Csvg viewBox=\'0 0 200 200\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cfilter id=\'noiseFilter\'%3E%3CfeTurbulence type=\'fractalNoise\' baseFrequency=\'0.8\' numOctaves=\'3\' stitchTiles=\'stitch\'/%3E%3C/filter%3E%3Crect width=\'100%25\' height=\'100%25\' filter=\'url(%23noiseFilter)\' opacity=\'0.15\'/%3E%3C/svg%3E")',
      },
      borderRadius: {
        DEFAULT: 'var(--radius)',
        chip: 'var(--radius-chip)',
        card: 'var(--radius-card)',
        'card-sm': 'var(--radius-card-sm)',
      },
      boxShadow: {
        'button': '4px 6px 0px 0px #4A3B2A',
        'soft': 'var(--shadow-soft)',
      },
    },
  },
  plugins: [],
}
