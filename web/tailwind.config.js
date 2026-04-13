/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        cream: '#f5f4ed',
        beige: '#e8e6dc',
        terracotta: '#d97757',
        charcoal: '#141413',
        'dark-surface': '#1e1e1c',
        secondary: '#30302e',
        muted: '#87867f',
        'muted-dark': '#a8a69e',
        border: '#c2c0b6',
        highlight: '#eda100',
      },
      fontFamily: {
        serif: ['"Source Serif 4 Variable"', 'Georgia', '"Times New Roman"', 'serif'],
        mono: ['"JetBrains Mono Variable"', '"Fira Code"', '"Consolas"', 'monospace'],
      },
    },
  },
  plugins: [],
};
