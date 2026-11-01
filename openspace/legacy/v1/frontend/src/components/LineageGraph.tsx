import { useMemo, useRef } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { SkillLineage } from '../api';

interface LineageGraphProps {
  lineage: SkillLineage;
  onNodeClick?: (skillId: string) => void;
}

type GraphNode = {
  id: string;
  name: string;
  score: number;
  generation: number;
  origin: string;
  is_active: boolean;
  x?: number;
  y?: number;
};

type GraphLink = {
  source: string;
  target: string;
};

export default function LineageGraph({ lineage, onNodeClick }: LineageGraphProps) {
  const fgRef = useRef<any>(null);

  const graphData = useMemo<{ nodes: GraphNode[]; links: GraphLink[] }>(
    () => ({
      nodes: lineage.nodes.map((node) => ({
        id: node.skill_id,
        name: node.name,
        score: node.score,
        generation: node.generation,
        origin: node.origin,
        is_active: node.is_active,
      })),
      links: lineage.edges.map((edge) => ({
        source: edge.source,
        target: edge.target,
      })),
    }),
    [lineage],
  );

  if (graphData.nodes.length === 0) {
    return <div className="text-sm text-muted">No lineage graph data.</div>;
  }

  return (
    <div className="panel-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-[color:var(--color-border)] text-sm font-bold">
        Skill Lineage
      </div>
      <div className="h-[420px]">
        <ForceGraph2D
          ref={fgRef}
          graphData={graphData}
          cooldownTicks={120}
          linkColor={() => 'rgba(20, 20, 19, 0.18)'}
          linkWidth={1.5}
          nodeLabel={(node) => {
            const skillNode = node as GraphNode;
            return `${skillNode.name}\nscore: ${skillNode.score.toFixed(1)}\norigin: ${skillNode.origin}`;
          }}
          onNodeClick={(node) => onNodeClick?.((node as GraphNode).id)}
          nodeCanvasObject={(node, ctx, globalScale) => {
            const skillNode = node as GraphNode;
            const label = skillNode.name;
            const size = 8 + Math.max(0, skillNode.score / 20);
            const fontSize = 12 / globalScale;
            const isActive = skillNode.is_active;
            ctx.beginPath();
            ctx.arc(skillNode.x ?? 0, skillNode.y ?? 0, size, 0, 2 * Math.PI, false);
            ctx.fillStyle = isActive ? '#D97757' : '#B8B4A8';
            ctx.fill();
            ctx.lineWidth = 2 / globalScale;
            ctx.strokeStyle = '#141413';
            ctx.stroke();
            ctx.font = `${fontSize}px ui-monospace`;
            ctx.fillStyle = '#141413';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, skillNode.x ?? 0, (skillNode.y ?? 0) + size + 4 / globalScale);
          }}
        />
      </div>
    </div>
  );
}
