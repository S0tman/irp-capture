// Theme toggle: system / light / dark
// Inline version runs in <head> to prevent flash (see BaseLayout)

export function getTheme(): 'light' | 'dark' {
  const stored = localStorage.getItem('theme');
  if (stored === 'dark') return 'dark';
  if (stored === 'light') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function setTheme(theme: 'light' | 'dark' | 'system') {
  if (theme === 'system') {
    localStorage.removeItem('theme');
  } else {
    localStorage.setItem('theme', theme);
  }
  applyTheme();
  // Notify React components to update their color schemes
  window.dispatchEvent(new CustomEvent('theme-changed'));
}

export function applyTheme() {
  const theme = getTheme();
  document.documentElement.classList.toggle('dark', theme === 'dark');
}

export function toggleTheme() {
  const current = getTheme();
  setTheme(current === 'dark' ? 'light' : 'dark');
}
