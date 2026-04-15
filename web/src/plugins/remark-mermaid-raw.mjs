/**
 * Remark plugin: replaces ```mermaid code blocks with slot divs.
 *
 * Each slot contains:
 *  - data-diagram-index    sequential index for React component teleport
 *  - data-mermaid-b64      base64-encoded mermaid source (safe in HTML attrs)
 *
 * Two rendering paths:
 *  A) If InteractiveDiagrams.astro maps a React component to this slot index,
 *     the teleport script moves it in and the b64 attr is ignored.
 *  B) Otherwise, the client-side mermaid.js script reads data-mermaid-b64,
 *     decodes with atob(), and renders the diagram.
 */
let diagramCounter = 0;

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
      // Use Buffer (Node.js) to base64-encode; safe in all HTML attribute contexts
      const b64 = Buffer.from(child.value, 'utf8').toString('base64');
      node.children[i] = {
        type: 'html',
        value: `<div class="diagram-slot mermaid-wrapper" data-diagram-index="${index}" data-mermaid-b64="${b64}"></div>`,
      };
    } else {
      walkTree(child);
    }
  }
}
