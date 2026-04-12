import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

interface Node {
  id: string;
  label: string;
  description: string;
  chapter: number;
  color: string;
}

const nodes: Node[] = [
  {
    id: 'ledger',
    label: 'Ledger',
    description: 'Immutable append-only source of truth. JSONL format, survives tool death.',
    chapter: 1,
    color: '#d97706',
  },
  {
    id: 'sensors',
    label: 'Sensors',
    description: 'Multi-source capture: Figma plugin, Slack, CLI, REST API. Tool-independent.',
    chapter: 3,
    color: '#ea580c',
  },
  {
    id: 'derivedstate',
    label: 'Derived State',
    description: 'Current.json: last 10 active decisions. Rebuilt from ledger, always consistent.',
    chapter: 2,
    color: '#f97316',
  },
  {
    id: 'validation',
    label: 'Validation',
    description: 'Check algorithm: keyword-based conflict detection without embeddings. Non-blocking.',
    chapter: 2,
    color: '#fb923c',
  },
  {
    id: 'restapis',
    label: 'REST APIs',
    description: '/inherit, /why, /check endpoints. Context injection for AI models. Sovereignty.',
    chapter: 6,
    color: '#fdba74',
  },
  {
    id: 'bridge',
    label: 'Bridge Pattern',
    description: '3-layer architecture: UI, bridge, core. Sandboxed integration, resilience.',
    chapter: 5,
    color: '#fcd34d',
  },
];

const relationships: Array<[string, string]> = [
  ['sensors', 'ledger'],
  ['ledger', 'derivedstate'],
  ['derivedstate', 'validation'],
  ['derivedstate', 'restapis'],
  ['sensors', 'bridge'],
  ['bridge', 'ledger'],
];

export default function IRPArchitectureExplorer({ className = '' }: { className?: string }) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  const displayedNodeId = selectedNode || hoveredNode;
  const displayedNode = nodes.find(n => n.id === displayedNodeId);

  const isConnected = (nodeId: string, targetId: string) => {
    return relationships.some(
      ([a, b]) => (a === nodeId && b === targetId) || (a === targetId && b === nodeId)
    );
  };

  const getConnectedNodes = (nodeId: string) => {
    const connected = new Set<string>();
    connected.add(nodeId);
    relationships.forEach(([a, b]) => {
      if (a === nodeId) connected.add(b);
      if (b === nodeId) connected.add(a);
    });
    return connected;
  };

  const connectedNodes = displayedNodeId ? getConnectedNodes(displayedNodeId) : new Set();

  return (
    <div className={`w-full ${className}`}>
      <div className="relative bg-white dark:bg-[var(--color-charcoal)] rounded-lg border border-[var(--color-border)] dark:border-[#333] p-8 min-h-96">
        {/* Grid of nodes */}
        <div className="grid grid-cols-2 md:grid-cols-3 gap-8 mb-8">
          {nodes.map((node) => {
            const isHighlighted = displayedNodeId && connectedNodes.has(node.id);
            const isFaded = displayedNodeId && !connectedNodes.has(node.id);

            return (
              <motion.button
                key={node.id}
                onClick={() => setSelectedNode(selectedNode === node.id ? null : node.id)}
                onHoverStart={() => setHoveredNode(node.id)}
                onHoverEnd={() => setHoveredNode(null)}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                className={`flex flex-col items-center gap-3 p-4 rounded-lg transition-all ${
                  isHighlighted
                    ? 'opacity-100'
                    : isFaded
                      ? 'opacity-40'
                      : 'opacity-100 hover:opacity-90'
                }`}
              >
                {/* Circle node */}
                <div
                  className={`w-20 h-20 rounded-full flex items-center justify-center text-white font-bold transition-all ${
                    selectedNode === node.id ? 'ring-2 ring-offset-2 ring-[var(--color-terracotta)]' : ''
                  }`}
                  style={{ backgroundColor: node.color }}
                >
                  <span className="text-center text-sm px-1">{node.label.split(' ')[0]}</span>
                </div>
                {/* Label */}
                <span className="text-xs font-semibold text-center text-[var(--color-charcoal)] dark:text-[var(--color-cream)] leading-tight">
                  {node.label}
                </span>
              </motion.button>
            );
          })}
        </div>

        {/* Description box */}
        <AnimatePresence>
          {displayedNode && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 10 }}
              className={`p-4 rounded-lg border ${
                selectedNode ? 'border-[var(--color-terracotta)]' : 'border-[var(--color-border)]'
              } dark:border-[#333] bg-[var(--color-beige)] dark:bg-[var(--color-dark-surface)]`}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-start justify-between mb-2">
                <h4 className="font-semibold text-[var(--color-charcoal)] dark:text-[var(--color-cream)]">
                  {displayedNode.label}
                </h4>
                {selectedNode && (
                  <button
                    onClick={() => setSelectedNode(null)}
                    className="text-[var(--color-muted)] hover:text-[var(--color-charcoal)] dark:hover:text-[var(--color-cream)] text-sm"
                    aria-label="Close"
                  >
                    ✕
                  </button>
                )}
              </div>
              <p className="text-sm text-[var(--color-secondary)] dark:text-[var(--color-muted)] mb-3">
                {displayedNode.description}
              </p>
              <a
                href={`/ch${displayedNode.chapter}-${
                  displayedNode.id === 'ledger' ? 'architecture' : displayedNode.id
                }/`}
                className="text-sm font-medium text-[var(--color-terracotta)] hover:underline"
              >
                Read Chapter {displayedNode.chapter} →
              </a>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Instructions */}
        <div className="mt-8 text-xs text-[var(--color-muted)] space-y-1">
          <p>
            <strong>Hover</strong> to see details. <strong>Click</strong> to lock description and click the link.
          </p>
          <p className="text-[var(--color-secondary)]">The six core abstractions of IRP and their relationships.</p>
        </div>
      </div>

      {/* Backdrop for dismissing */}
      {selectedNode && (
        <div
          className="fixed inset-0 z-0"
          onClick={() => setSelectedNode(null)}
          style={{ pointerEvents: 'auto' }}
        />
      )}
    </div>
  );
}
