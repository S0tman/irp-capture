import React, { useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import { motion } from 'framer-motion';

interface Node {
  id: string;
  label: string;
  shortLabel: string;
  description: string;
  chapter: number;
  color: string;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

interface Link {
  source: string;
  target: string;
  label: string;
}

const nodes: Node[] = [
  {
    id: 'ledger',
    label: 'Ledger',
    shortLabel: 'L',
    description: 'Immutable append-only source of truth. JSONL format, survives tool death.',
    chapter: 1,
    color: '#d97706',
  },
  {
    id: 'sensors',
    label: 'Sensors',
    shortLabel: 'S',
    description: 'Multi-source capture: Figma plugin, Slack, CLI, REST API. Tool-independent.',
    chapter: 3,
    color: '#ea580c',
  },
  {
    id: 'derivedstate',
    label: 'Derived State',
    shortLabel: 'DS',
    description: 'Current.json: last 10 active decisions. Rebuilt from ledger, always consistent.',
    chapter: 2,
    color: '#f97316',
  },
  {
    id: 'validation',
    label: 'Validation',
    shortLabel: 'V',
    description: 'Check algorithm: keyword-based conflict detection without embeddings. Non-blocking.',
    chapter: 2,
    color: '#fb923c',
  },
  {
    id: 'restapis',
    label: 'REST APIs',
    shortLabel: 'REST',
    description: '/inherit, /why, /check endpoints. Context injection for AI models. Sovereignty.',
    chapter: 6,
    color: '#fdba74',
  },
  {
    id: 'bridge',
    label: 'Bridge Pattern',
    shortLabel: 'BP',
    description: '3-layer architecture: UI, bridge, core. Sandboxed integration, resilience.',
    chapter: 5,
    color: '#fcd34d',
  },
];

const links: Link[] = [
  { source: 'sensors', target: 'ledger', label: 'appends' },
  { source: 'ledger', target: 'derivedstate', label: 'rebuilds' },
  { source: 'derivedstate', target: 'validation', label: 'checks' },
  { source: 'derivedstate', target: 'restapis', label: 'serves' },
  { source: 'sensors', target: 'bridge', label: 'through' },
  { source: 'bridge', target: 'ledger', label: 'feeds' },
];

export default function IRPArchitectureExplorer({ className = '' }: { className?: string }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const simulationRef = useRef<d3.Simulation<Node, Link> | null>(null);

  useEffect(() => {
    if (!svgRef.current) return;

    const width = svgRef.current.clientWidth;
    const height = 500;

    // Clear previous
    d3.select(svgRef.current).selectAll('*').remove();

    const svg = d3.select(svgRef.current)
      .attr('width', width)
      .attr('height', height);

    // Create simulation
    const simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink<Node, Link>(links)
        .id((d: any) => d.id)
        .distance(120)
        .strength(0.5))
      .force('charge', d3.forceManyBody().strength(-300))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(60));

    simulationRef.current = simulation;

    // Links
    const link = svg.append('g')
      .selectAll('line')
      .data(links)
      .enter()
      .append('line')
      .attr('stroke', 'rgba(0,0,0,0.1)')
      .attr('stroke-width', 2)
      .attr('marker-end', 'url(#arrowhead)');

    // Arrow marker
    svg.append('defs').append('marker')
      .attr('id', 'arrowhead')
      .attr('markerWidth', 10)
      .attr('markerHeight', 10)
      .attr('refX', 24)
      .attr('refY', 3)
      .attr('orient', 'auto')
      .append('polygon')
      .attr('points', '0 0, 10 3, 0 6')
      .attr('fill', 'rgba(0,0,0,0.1)');

    // Link labels
    const linkLabel = svg.append('g')
      .selectAll('text')
      .data(links)
      .enter()
      .append('text')
      .attr('font-size', 11)
      .attr('fill', 'rgba(0,0,0,0.5)')
      .attr('text-anchor', 'middle')
      .text((d: any) => d.label);

    // Nodes
    const node = svg.append('g')
      .selectAll('circle')
      .data(nodes)
      .enter()
      .append('circle')
      .attr('r', 45)
      .attr('fill', (d: any) => d.color)
      .attr('stroke', 'white')
      .attr('stroke-width', 2)
      .style('cursor', 'pointer')
      .on('mouseenter', (event, d: any) => setHoveredNode(d.id))
      .on('mouseleave', () => setHoveredNode(null))
      .on('click', (event, d: any) => {
        event.stopPropagation();
        setSelectedNode(selectedNode === d.id ? null : d.id);
      })
      .call(d3.drag<SVGCircleElement, Node>()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        }));

    // Node labels
    const labels = svg.append('g')
      .selectAll('text')
      .data(nodes)
      .enter()
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', '0.3em')
      .attr('font-weight', 'bold')
      .attr('font-size', 16)
      .attr('fill', 'white')
      .text((d: any) => d.shortLabel)
      .style('pointer-events', 'none');

    // Update positions on simulation tick
    simulation.on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x!)
        .attr('y1', (d: any) => d.source.y!)
        .attr('x2', (d: any) => d.target.x!)
        .attr('y2', (d: any) => d.target.y!);

      linkLabel
        .attr('x', (d: any) => (d.source.x! + d.target.x!) / 2)
        .attr('y', (d: any) => (d.source.y! + d.target.y!) / 2);

      node
        .attr('cx', (d: any) => d.x!)
        .attr('cy', (d: any) => d.y!);

      labels
        .attr('x', (d: any) => d.x!)
        .attr('y', (d: any) => d.y!);
    });

    // Highlight connected nodes on hover
    const highlightConnected = (nodeId: string | null) => {
      if (!nodeId) {
        node.attr('opacity', 1);
        link.attr('opacity', 0.3).attr('stroke', 'rgba(0,0,0,0.1)');
        linkLabel.attr('opacity', 0.5);
        return;
      }

      const connectedIds = new Set<string>();
      connectedIds.add(nodeId);

      links.forEach(l => {
        if ((l.source as any).id === nodeId) connectedIds.add((l.target as any).id);
        if ((l.target as any).id === nodeId) connectedIds.add((l.source as any).id);
      });

      node.attr('opacity', (d: any) => connectedIds.has(d.id) ? 1 : 0.3);
      link.attr('opacity', (d: any) => {
        const sourceId = typeof d.source === 'string' ? d.source : (d.source as any).id;
        const targetId = typeof d.target === 'string' ? d.target : (d.target as any).id;
        return (sourceId === nodeId || targetId === nodeId) ? 1 : 0.1;
      }).attr('stroke', (d: any) => {
        const sourceId = typeof d.source === 'string' ? d.source : (d.source as any).id;
        const targetId = typeof d.target === 'string' ? d.target : (d.target as any).id;
        if (sourceId === nodeId || targetId === nodeId) {
          return 'rgba(217, 119, 6, 0.3)';
        }
        return 'rgba(0,0,0,0.05)';
      });
      linkLabel.attr('opacity', (d: any) => {
        const sourceId = typeof d.source === 'string' ? d.source : (d.source as any).id;
        const targetId = typeof d.target === 'string' ? d.target : (d.target as any).id;
        return (sourceId === nodeId || targetId === nodeId) ? 1 : 0.1;
      });
    };

    // Watch for hover/selection changes
    highlightConnected(displayedNode);

    return () => {
      simulation.stop();
    };
  }, [displayedNode, hoveredNode, selectedNode]);

  const displayedNode = selectedNode || hoveredNode;
  const displayedNodeData = nodes.find(n => n.id === displayedNode);

  return (
    <div className={`w-full ${className}`}>
      <svg ref={svgRef} className="w-full border border-[var(--color-border)] dark:border-[#333] rounded-lg bg-white dark:bg-[var(--color-charcoal)]" />

      {displayedNodeData && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 10 }}
          className={`mt-4 p-4 rounded-lg border ${selectedNode ? 'border-[var(--color-terracotta)]' : 'border-[var(--color-border)]'} dark:border-[#333] bg-[var(--color-beige)] dark:bg-[var(--color-dark-surface)]`}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-start justify-between mb-2">
            <h4 className="font-semibold text-[var(--color-charcoal)] dark:text-[var(--color-cream)]">
              {displayedNodeData.label}
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
            {displayedNodeData.description}
          </p>
          <a
            href={`/ch${displayedNodeData.chapter}-${nodes.find(n => n.id === 'ledger')?.id === displayedNodeData.id ? 'architecture' : displayedNodeData.id}/`}
            className="text-sm font-medium text-[var(--color-terracotta)] hover:underline"
          >
            Read Chapter {displayedNodeData.chapter} →
          </a>
        </motion.div>
      )}

      {selectedNode && (
        <div
          className="fixed inset-0 z-0"
          onClick={() => setSelectedNode(null)}
          style={{ pointerEvents: 'auto' }}
        />
      )}

      <div className="mt-4 text-xs text-[var(--color-muted)] space-y-1">
        <p><strong>Drag nodes</strong> to rearrange. <strong>Hover</strong> for details. <strong>Click</strong> to read the chapter.</p>
        <p className="text-[var(--color-secondary)]">The six core abstractions of IRP and how they connect.</p>
      </div>
    </div>
  );
}
