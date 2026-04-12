import React, { useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import { motion } from 'framer-motion';

interface Node extends d3.SimulationNodeDatum {
  id: string;
  label: string;
  shortLabel: string;
  description: string;
  chapter: number;
  color: string;
}

interface Link extends d3.SimulationLinkDatum<Node> {
  label?: string;
}

const nodesData: Omit<Node, 'index' | 'x' | 'y' | 'vx' | 'vy' | 'fx' | 'fy'>[] = [
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

const linksData: Array<{ source: string; target: string; label: string }> = [
  { source: 'sensors', target: 'ledger', label: 'appends' },
  { source: 'ledger', target: 'derivedstate', label: 'rebuilds' },
  { source: 'derivedstate', target: 'validation', label: 'checks' },
  { source: 'derivedstate', target: 'restapis', label: 'serves' },
  { source: 'sensors', target: 'bridge', label: 'through' },
  { source: 'bridge', target: 'ledger', label: 'feeds' },
];

export default function IRPArchitectureExplorer({ className = '' }: { className?: string }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ x: number; y: number } | null>(null);
  const simulationRef = useRef<d3.Simulation<Node, Link> | null>(null);

  useEffect(() => {
    if (!svgRef.current || !containerRef.current) return;

    const container = containerRef.current;
    const width = container.clientWidth || 900;
    const height = 600;

    // Only reinitialize if SVG is empty
    const existingSVG = d3.select(svgRef.current);
    if (existingSVG.selectAll('circle').size() > 0) {
      return;
    }

    // Clear previous content
    existingSVG.selectAll('*').remove();

    // Create SVG
    const svg = d3.select(svgRef.current)
      .attr('width', width)
      .attr('height', height)
      .style('background', 'transparent');

    // Store hover state in closure, not React state
    let hoveredNodeId: string | null = null;

    // Create nodes array with proper typing
    const nodes: Node[] = nodesData.map(d => ({ ...d })) as Node[];
    const links: Link[] = linksData.map(d => ({
      source: nodes.find(n => n.id === d.source)!,
      target: nodes.find(n => n.id === d.target)!,
      label: d.label,
    }));

    // Create simulation
    const simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links)
        .id((d: any) => d.id)
        .distance(140)
        .strength(0.5))
      .force('charge', d3.forceManyBody().strength(-400))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(55));

    simulationRef.current = simulation;

    // Arrowhead marker
    svg.append('defs').append('marker')
      .attr('id', 'arrowhead')
      .attr('markerWidth', 10)
      .attr('markerHeight', 10)
      .attr('refX', 24)
      .attr('refY', 3)
      .attr('orient', 'auto')
      .append('polygon')
      .attr('points', '0 0, 10 3, 0 6')
      .attr('fill', 'rgba(217, 119, 6, 0.6)');

    // Arrowhead marker for highlighted connections
    svg.append('defs').append('marker')
      .attr('id', 'arrowhead-highlight')
      .attr('markerWidth', 10)
      .attr('markerHeight', 10)
      .attr('refX', 24)
      .attr('refY', 3)
      .attr('orient', 'auto')
      .append('polygon')
      .attr('points', '0 0, 10 3, 0 6')
      .attr('fill', 'rgba(217, 119, 6, 1)');

    // Links
    const link = svg.append('g')
      .selectAll('line')
      .data(links)
      .enter()
      .append('line')
      .attr('stroke', 'rgba(217, 119, 6, 0.2)')
      .attr('stroke-width', 2)
      .attr('marker-end', 'url(#arrowhead)')
      .style('opacity', 0.6);

    // Link labels
    const linkLabel = svg.append('g')
      .selectAll('text')
      .data(links)
      .enter()
      .append('text')
      .attr('font-size', 11)
      .attr('fill', 'rgba(217, 119, 6, 0.5)')
      .attr('text-anchor', 'middle')
      .attr('dy', -5)
      .text((d: any) => d.label);

    // Update visualization based on hover state
    const updateVisualization = (target: string | null) => {
      node.attr('opacity', (d: any) => {
        if (!target && !selectedNode) return 1;
        const activeTarget = selectedNode || target;
        if (d.id === activeTarget) return 1;
        const isConnected = links.some(
          (l: any) => (l.source.id === activeTarget && l.target.id === d.id) ||
                      (l.target.id === activeTarget && l.source.id === d.id)
        );
        return isConnected ? 1 : 0.3;
      })
        .style('filter', (d: any) => {
          if (selectedNode === d.id) {
            return 'drop-shadow(0 0 8px rgba(217, 119, 6, 0.6))';
          }
          return 'none';
        });

      link.style('opacity', (d: any) => {
        if (!target && !selectedNode) return 0.3;
        const activeTarget = selectedNode || target;
        return ((d.source.id === activeTarget) || (d.target.id === activeTarget)) ? 0.8 : 0.1;
      })
        .attr('stroke', (d: any) => {
          const activeTarget = selectedNode || target;
          return ((d.source.id === activeTarget) || (d.target.id === activeTarget))
            ? 'rgba(217, 119, 6, 0.6)'
            : 'rgba(217, 119, 6, 0.2)';
        })
        .attr('marker-end', (d: any) => {
          const activeTarget = selectedNode || target;
          return ((d.source.id === activeTarget) || (d.target.id === activeTarget))
            ? 'url(#arrowhead-highlight)'
            : 'url(#arrowhead)';
        });

      linkLabel.style('opacity', (d: any) => {
        if (!target && !selectedNode) return 0.5;
        const activeTarget = selectedNode || target;
        return ((d.source.id === activeTarget) || (d.target.id === activeTarget)) ? 1 : 0.1;
      });
    };

    // Nodes
    const node = svg.append('g')
      .selectAll('circle')
      .data(nodes)
      .enter()
      .append('circle')
      .attr('r', 45)
      .attr('fill', (d: any) => d.color)
      .attr('stroke', 'white')
      .attr('stroke-width', 2.5)
      .style('cursor', 'grab')
      .on('mouseenter', (event, d: any) => {
        hoveredNodeId = d.id;
        updateVisualization(hoveredNodeId);
        setHoveredNode(d.id);
        // Show tooltip
        const nodeData = nodesData.find(n => n.id === d.id);
        if (nodeData && tooltipRef.current) {
          setTooltipPos({ x: event.clientX, y: event.clientY });
        }
      })
      .on('mousemove', (event, d: any) => {
        // Update tooltip position as mouse moves
        if (tooltipRef.current && tooltipRef.current.style.display !== 'none') {
          setTooltipPos({ x: event.clientX, y: event.clientY });
        }
      })
      .on('mouseleave', () => {
        hoveredNodeId = null;
        updateVisualization(null);
        setHoveredNode(null);
        setTooltipPos(null);
      })
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

    // Node labels - show full labels on circles
    const labels = svg.append('g')
      .selectAll('text')
      .data(nodes)
      .enter()
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', '0.3em')
      .attr('font-weight', 'bold')
      .attr('font-size', (d: any) => {
        // Adjust font size based on label length to fit in circle
        const labelLength = d.label.length;
        return labelLength > 10 ? 11 : 13;
      })
      .attr('fill', 'white')
      .text((d: any) => d.label)
      .style('pointer-events', 'none');

    // Update positions on tick
    simulation.on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x)
        .attr('y1', (d: any) => d.source.y)
        .attr('x2', (d: any) => d.target.x)
        .attr('y2', (d: any) => d.target.y);

      linkLabel
        .attr('x', (d: any) => (d.source.x + d.target.x) / 2)
        .attr('y', (d: any) => (d.source.y + d.target.y) / 2);

      node
        .attr('cx', (d: any) => Math.max(45, Math.min(width - 45, d.x)))
        .attr('cy', (d: any) => Math.max(45, Math.min(height - 45, d.y)));
    });

    return () => {
      simulation.stop();
    };
  }, [selectedNode]);

  const displayedNode = nodesData.find(n => n.id === (selectedNode || hoveredNode));
  const tooltipNode = hoveredNode ? nodesData.find(n => n.id === hoveredNode) : null;

  return (
    <div className={`w-full ${className} relative`}>
      <div ref={containerRef} className="w-full rounded-lg border border-[var(--color-border)] dark:border-[#333] bg-white dark:bg-[var(--color-charcoal)] overflow-hidden">
        <svg
          ref={svgRef}
          className="w-full"
          style={{ display: 'block' }}
        />
      </div>

      {/* Hover Tooltip */}
      {tooltipNode && tooltipPos && !selectedNode && (
        <div
          ref={tooltipRef}
          className="fixed bg-[var(--color-charcoal)] dark:bg-[var(--color-cream)] text-white dark:text-[var(--color-charcoal)] px-3 py-2 rounded-lg text-sm max-w-xs pointer-events-none z-50 shadow-lg border border-[var(--color-terracotta)]"
          style={{
            left: `${tooltipPos.x + 10}px`,
            top: `${tooltipPos.y + 10}px`,
          }}
        >
          <p className="font-semibold mb-1">{tooltipNode.label}</p>
          <p className="text-xs opacity-90">{tooltipNode.description}</p>
        </div>
      )}

      {displayedNode && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 10 }}
          className={`mt-4 p-4 rounded-lg border-2 transition-all ${
            selectedNode
              ? 'border-[var(--color-terracotta)] bg-[var(--color-beige)] dark:bg-[#2a2a2a]'
              : 'border-[var(--color-border)] bg-[var(--color-beige)]/70 dark:bg-[var(--color-dark-surface)]/70'
          } dark:border-[#333]`}
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

      <div className="mt-4 text-xs text-[var(--color-muted)] space-y-1">
        <p><strong>Drag nodes</strong> to rearrange. <strong>Hover</strong> for details. <strong>Click</strong> to lock and read the chapter.</p>
        <p className="text-[var(--color-secondary)]">The six core abstractions of IRP and their relationships.</p>
      </div>
    </div>
  );
}
