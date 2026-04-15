/**
 * Remark plugin: re-labels ```mermaid code blocks as ```plaintext
 * so Shiki can handle them without mermaid grammar.
 *
 * The client-side script in ChapterLayout.astro finds code blocks
 * whose text starts with mermaid directives and renders them.
 *
 * Minimal mutation: no new nodes, no hProperties, no raw HTML.
 * Just changes child.lang in-place.
 */
export default function remarkMermaidRaw() {
  return (tree) => {
    walkTree(tree);
  };
}

function walkTree(node) {
  if (!node.children) return;

  for (let i = 0; i < node.children.length; i++) {
    const child = node.children[i];
    if (child.type === 'code' && child.lang === 'mermaid') {
      // Minimal in-place mutation — just relabel the language
      child.lang = 'plaintext';
      child.meta = 'mermaid';
    } else {
      walkTree(child);
    }
  }
}
