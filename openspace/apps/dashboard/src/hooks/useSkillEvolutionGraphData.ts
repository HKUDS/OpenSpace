import { useEffect, useMemo, useRef } from 'react';
import type { SkillLineage, SkillLineageNode } from '../api';

export interface SkillGraphNode {
  id: string;
  name: string;
  score: number;
  origin: string;
  generation: number;
  created_at: string;
  visibility: string;
  totalSelections: number;
  effectiveRate: number;
  isActive: boolean;
  tags: string[];
  usageRatio: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

interface SkillGraphLink {
  source: string;
  target: string;
}

interface UseSkillEvolutionGraphDataResult {
  allOrigins: string[];
  allTags: string[];
  graphData: {
    nodes: SkillGraphNode[];
    links: SkillGraphLink[];
  };
}

function createGraphNode(node: SkillLineageNode, maxSelections: number): SkillGraphNode {
  const usageRatio = maxSelections > 0 ? 0.25 + 0.75 * (node.total_selections / maxSelections) : 0.35;
  return {
    id: node.skill_id,
    name: node.name || node.skill_id.slice(0, 8),
    score: node.score,
    origin: node.origin,
    generation: node.generation,
    created_at: node.created_at,
    visibility: node.visibility,
    totalSelections: node.total_selections,
    effectiveRate: node.effective_rate,
    isActive: node.is_active,
    tags: node.tags,
    usageRatio,
  };
}

export function useSkillEvolutionGraphData(
  lineage: SkillLineage | null,
  originFilter: string,
  tagFilter: string,
  alwaysVisibleSkillIds: string[] = [],
): UseSkillEvolutionGraphDataResult {
  const cachedNodesByLineageRef = useRef(new Map<string, Map<string, SkillGraphNode>>());

  const allOrigins = useMemo(() => {
    if (!lineage) {
      return [];
    }
    return Array.from(new Set(lineage.nodes.map((node) => node.origin))).sort();
  }, [lineage]);

  const allTags = useMemo(() => {
    if (!lineage) {
      return [];
    }
    const tags = new Set<string>();
    lineage.nodes.forEach((node) => {
      node.tags.forEach((tag) => tags.add(tag));
    });
    return Array.from(tags).sort();
  }, [lineage]);

  const filteredLineage = useMemo(() => {
    if (!lineage) {
      return null;
    }

    const pinnedSkillIds = new Set(alwaysVisibleSkillIds.filter(Boolean));
    const visibleNodeIds = new Set<string>();
    const filteredNodes = lineage.nodes.filter((node) => {
      if (pinnedSkillIds.has(node.skill_id)) {
        return true;
      }
      if (originFilter !== 'all' && node.origin !== originFilter) {
        return false;
      }
      if (tagFilter !== 'all' && !node.tags.includes(tagFilter)) {
        return false;
      }
      return true;
    });

    filteredNodes.forEach((node) => visibleNodeIds.add(node.skill_id));

    const filteredEdges = lineage.edges.filter(
      (edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target),
    );

    return {
      skill_id: lineage.skill_id,
      nodes: filteredNodes,
      edges: filteredEdges,
      total_nodes: filteredNodes.length,
    };
  }, [alwaysVisibleSkillIds, lineage, originFilter, tagFilter]);

  const lineageNodeIds = useMemo(
    () => new Set((lineage?.nodes ?? []).map((node) => node.skill_id)),
    [lineage],
  );

  const currentLineageId = filteredLineage?.skill_id ?? null;

  const { graphData, visibleNodes } = useMemo(() => {
    if (!filteredLineage) {
      return {
        graphData: { nodes: [], links: [] },
        visibleNodes: new Map<string, SkillGraphNode>(),
      };
    }

    const activeCache = currentLineageId
      ? cachedNodesByLineageRef.current.get(currentLineageId) ?? new Map<string, SkillGraphNode>()
      : new Map<string, SkillGraphNode>();
    const maxSelections = Math.max(...filteredLineage.nodes.map((node) => node.total_selections), 0);
    const nextVisibleNodes = new Map<string, SkillGraphNode>();

    const nodes = filteredLineage.nodes.map((node) => {
      const existingNode = activeCache.get(node.skill_id);
      const graphNode = existingNode ?? createGraphNode(node, maxSelections);
      const freshNode = createGraphNode(node, maxSelections);

      graphNode.id = freshNode.id;
      graphNode.name = freshNode.name;
      graphNode.score = freshNode.score;
      graphNode.origin = freshNode.origin;
      graphNode.generation = freshNode.generation;
      graphNode.created_at = freshNode.created_at;
      graphNode.visibility = freshNode.visibility;
      graphNode.totalSelections = freshNode.totalSelections;
      graphNode.effectiveRate = freshNode.effectiveRate;
      graphNode.isActive = freshNode.isActive;
      graphNode.tags = freshNode.tags;
      graphNode.usageRatio = freshNode.usageRatio;

      nextVisibleNodes.set(node.skill_id, graphNode);
      return graphNode;
    });

    return {
      graphData: {
        nodes,
        links: filteredLineage.edges.map((edge) => ({
          source: edge.source,
          target: edge.target,
        })),
      },
      visibleNodes: nextVisibleNodes,
    };
  }, [currentLineageId, filteredLineage]);

  useEffect(() => {
    if (!currentLineageId) {
      return;
    }

    const nextLineageCache = new Map(
      cachedNodesByLineageRef.current.get(currentLineageId) ?? new Map<string, SkillGraphNode>(),
    );

    visibleNodes.forEach((node, nodeId) => {
      nextLineageCache.set(nodeId, node);
    });

    for (const nodeId of nextLineageCache.keys()) {
      if (!lineageNodeIds.has(nodeId)) {
        nextLineageCache.delete(nodeId);
      }
    }

    const nextCaches = new Map(cachedNodesByLineageRef.current);
    nextCaches.set(currentLineageId, nextLineageCache);
    cachedNodesByLineageRef.current = nextCaches;
  }, [currentLineageId, lineageNodeIds, visibleNodes]);

  return { allOrigins, allTags, graphData };
}
