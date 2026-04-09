// IRP Capture — Figma plugin main (runs in plugin sandbox)
// Handles Figma API access and relays to/from the UI iframe.

figma.showUI(__html__, { width: 360, height: 480, title: "IRP Capture" });

// Receive messages from ui.html
figma.ui.onmessage = async (msg) => {
  if (msg.type === "get-context") {
    // Send current page and selection context to the UI
    const page = figma.currentPage.name;
    const selection = figma.currentPage.selection;
    const selectionLabel = selection.length > 0
      ? selection.map(n => n.name).join(", ")
      : null;

    figma.ui.postMessage({
      type: "context",
      page,
      selection: selectionLabel
    });
  }

  if (msg.type === "captured") {
    figma.notify("Decision captured to IRP ✓");
  }

  if (msg.type === "error") {
    figma.notify("IRP bridge not reachable — is the bridge running?", { error: true });
  }
};
