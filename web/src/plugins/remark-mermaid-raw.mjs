/**
 * Remark plugin: replaces ```mermaid code blocks with slot divs.
 *
 * Each slot contains:
 *  - data-diagram-index    sequential index for React component teleport
 *  - <template class="mermaid-src">  the raw mermaid source, HTML-encoded
 *
 * Two rendering paths:
 *  A) If InteractiveDiagrams.astro maps a React component to this slot index,
 *     the teleport script moves it in and the template is ignored.
 *  B) Otherwise, the client-side mermaid.js script reads the template and
 *     renders the diagram directly.
 */
let diagramCounter = 0;

function htmlEncode(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export default function remarkMermaidRaw() {
  return (tree) => {
    diagramCounter = 0;
    walkTree(tree);
  };
}

function walkTree(node) {
  if (!node.children) return;

  for (let i = 0; i < node.children.length; i++) {
    const child = node.children[i];
    if (child.type === 'code' && child.lang === 'mermaid') {
      const index = diagramCounter++;
      node.children[i] = {
        type: 'html',
        value: `<div class="diagram-slot mermaid-wrapper" data-diagram-index="${index}"><template class="mermaid-src">${htmlEncode(child.value)}</template></div>`,
      };
    } else {
      walkTree(child);
    }
  }
}
