import { packEnclose, packSiblings } from "d3-hierarchy";

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from "d3-force";

function components(nodes, edges) {
  const neighbors = new Map(nodes.map((node) => [node.id, []]));
  edges.forEach((edge) => {
    neighbors.get(edge.from).push(edge.to);
    neighbors.get(edge.to).push(edge.from);
  });
  const remaining = new Set(neighbors.keys());
  const result = [];
  nodes.forEach((node) => {
    if (!remaining.has(node.id)) return;
    const members = [];
    const stack = [node.id];
    remaining.delete(node.id);
    while (stack.length) {
      const id = stack.pop();
      members.push(id);
      neighbors.get(id).forEach((neighbor) => {
        if (remaining.delete(neighbor)) stack.push(neighbor);
      });
    }
    result.push(members);
  });
  return result;
}

function arrange(members, edges, seed) {
  const schools = [...new Set(members.map((node) => node.school_id))].sort(
    (a, b) => a - b,
  );
  const radius = schools.length > 1 ? Math.sqrt(members.length) * 10 : 0;
  const centers = new Map(
    schools.map((school, index) => {
      const angle = (index * 2 * Math.PI) / schools.length + seed;
      return [
        school,
        { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius },
      ];
    }),
  );
  const nodes = members.map((node, index) => {
    const center = centers.get(node.school_id);
    const angle = index * 2.39996 + seed;
    const spread = Math.sqrt(index % 60) * 8;
    return {
      id: node.id,
      school_id: node.school_id,
      x: center.x + Math.cos(angle) * spread,
      y: center.y + Math.sin(angle) * spread,
    };
  });
  const links = edges.map((edge) => ({
    source: edge.from,
    target: edge.to,
    tournaments: edge.tournaments,
  }));
  const simulation = forceSimulation(nodes)
    .force("charge", forceManyBody().strength(-70).distanceMax(180))
    .force("collision", forceCollide(14).iterations(2))
    .force(
      "link",
      forceLink(links)
        .id((node) => node.id)
        .distance((edge) => 70 / Math.sqrt(Math.max(1, edge.tournaments)))
        .strength((edge) => Math.min(0.65, 0.12 + edge.tournaments * 0.035)),
    )
    .force(
      "schoolX",
      forceX((node) => centers.get(node.school_id).x).strength(0.035),
    )
    .force(
      "schoolY",
      forceY((node) => centers.get(node.school_id).y).strength(0.035),
    )
    .velocityDecay(0.65)
    .stop();
  const frames = [];
  simulation.tick(20);
  for (let step = 0; step < 24; step += 1) {
    simulation.tick(10);
    frames.push(nodes.map((node) => ({ id: node.id, x: node.x, y: node.y })));
  }
  const left = Math.min(...nodes.map((node) => node.x)) - 8;
  const top = Math.min(...nodes.map((node) => node.y)) - 8;
  const width = Math.max(...nodes.map((node) => node.x)) - left + 8;
  const height = Math.max(...nodes.map((node) => node.y)) - top + 8;
  return { nodes, frames, left, top, width, height };
}

// eslint-disable-next-line no-restricted-globals
const workerScope = self;
workerScope.onmessage = ({ data }) => {
  const byId = new Map(data.nodes.map((node) => [node.id, node]));
  const groups = components(data.nodes, data.edges);
  const groupByNode = new Map();
  groups.forEach((group, index) =>
    group.forEach((id) => groupByNode.set(id, index)),
  );
  const edgesByGroup = groups.map(() => []);
  data.edges.forEach((edge) =>
    edgesByGroup[groupByNode.get(edge.from)].push(edge),
  );
  const layouts = groups.map((group, index) =>
    arrange(
      group.map((id) => byId.get(id)),
      edgesByGroup[index],
      data.seed,
    ),
  );
  const circles = layouts.map((layout) => {
    const centerX = layout.left + layout.width / 2;
    const centerY = layout.top + layout.height / 2;
    const radius = Math.max(
      ...layout.nodes.map((node) =>
        Math.hypot(node.x - centerX, node.y - centerY),
      ),
    );
    return { r: radius + 16, layout };
  });
  packSiblings(circles);
  const center = packEnclose(circles);
  const frames = Array.from({ length: 24 }, () => []);
  circles.forEach((circle) => {
    const { layout } = circle;
    const offsetX = circle.x - center.x - layout.left - layout.width / 2;
    const offsetY = circle.y - center.y - layout.top - layout.height / 2;
    layout.frames.forEach((frame, index) => {
      frame.forEach((node) =>
        frames[index].push({
          id: node.id,
          x: node.x + offsetX,
          y: node.y + offsetY,
        }),
      );
    });
  });
  workerScope.postMessage({ frames });
};
