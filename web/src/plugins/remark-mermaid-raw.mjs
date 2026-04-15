/**
 * Remark plugin: converts ```mermaid code blocks into plain-text code blocks
 * with data attributes for client-side mermaid rendering.
 *
 * Strategy: Astro 5's content layer strips `type: 'html'` raw nodes.
 * Instead, we keep legitimate `type: 'code'` MDAST nodes (which Shiki
 * and rehype always preserve) and mark them with hProperties so the
 * client-side script can find and render them.
 *
 * The mermaid source stays visible as a <pre><code> block until JS
 * replaces it — graceful degradation by design.
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
      // Keep as a code block but switch lang to 'text' so Shiki doesn't
      // need mermaid grammar. Add hProperties that remark-rehype passes
      // through to the generated <code> element.
      node.children[i] = {
        type: 'code',
        lang: 'text',
        value: child.value,
        data: {
          hProperties: {
            className: ['mermaid-source'],
            datadiagramindex: String(index),
          },
        },
      };
    } else {
      walkTree(child);
    }
  }
}
