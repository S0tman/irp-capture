// Focus mode: toggle sidebar and TOC visibility for distraction-free reading
const layout = document.getElementById('chapter-layout');
const sidebar = document.getElementById('sidebar');
const toc = document.querySelector('aside');
const expandIcon = document.getElementById('focus-icon-expand');
const collapseIcon = document.getElementById('focus-icon-collapse');
const toggleBtn = document.getElementById('focus-toggle');

function applyFocusMode(active: boolean) {
  if (layout) layout.classList.toggle('focus-mode', active);
  if (sidebar) sidebar.classList.toggle('lg:hidden', active);
  if (sidebar) sidebar.classList.toggle('lg:block', !active);
  if (toc) toc.classList.toggle('xl:hidden', active);
  if (toc) toc.classList.toggle('xl:block', !active);

  if (expandIcon && collapseIcon) {
    expandIcon.classList.toggle('hidden', active);
    collapseIcon.classList.toggle('hidden', !active);
  }
}

// Restore from localStorage
const stored = localStorage.getItem('focus-mode');
if (stored === 'true') {
  applyFocusMode(true);
}

toggleBtn?.addEventListener('click', () => {
  const isActive = layout?.classList.contains('focus-mode') ?? false;
  const newState = !isActive;
  applyFocusMode(newState);
  localStorage.setItem('focus-mode', String(newState));
});
