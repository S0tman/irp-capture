// Add copy buttons to all code blocks
document.querySelectorAll('pre').forEach((pre) => {
  const wrapper = document.createElement('div');
  wrapper.className = 'code-block-wrapper';
  pre.parentNode?.insertBefore(wrapper, pre);
  wrapper.appendChild(pre);

  const btn = document.createElement('button');
  btn.className = 'copy-button';
  btn.textContent = 'Copy';
  btn.addEventListener('click', async () => {
    const code = pre.querySelector('code')?.textContent || pre.textContent || '';
    try {
      await navigator.clipboard.writeText(code);
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
    } catch {
      btn.textContent = 'Failed';
      setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
    }
  });
  wrapper.appendChild(btn);
});
