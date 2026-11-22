import "../css/partnership_network.scss";

import {
  attendanceRadius,
  partnershipWidth,
} from "./partnership_network_styles";
import setupSelection from "./partnership_network_selection";
import setupMotion from "./partnership_network_motion";
import setupAxes from "./partnership_network_axes";

async function initPartnershipNetwork() {
  const root = document.getElementById("partnership-network");
  if (!root) return;
  const status = root.querySelector("[data-network-status]");
  const canvas = root.querySelector(".partnership-network-canvas");
  const pause = root.querySelector("[data-network-pause]");
  try {
    const [response, { Network, DataSet }] = await Promise.all([
      fetch(root.dataset.url),
      import("vis-network/standalone/esm/vis-network.js"),
    ]);
    if (!response.ok) throw new Error("Unable to load network");
    const data = await response.json();
    const peopleCount = data.nodes.length.toLocaleString();
    const pairCount = data.edges.length.toLocaleString();
    status.textContent = `${peopleCount} people · ${pairCount} partnerships`;
    if (!data.nodes.length) {
      root.querySelector("[data-network-details]").textContent =
        "No partnerships match these filters. Try a wider season range or fewer filters.";
      canvas.hidden = true;
      return;
    }
    const schools = [...new Set(data.nodes.map((node) => node.school_id))].sort(
      (a, b) => a - b,
    );
    const colors = new Map(
      schools.map((school, index) => [
        school,
        `hsl(${(index * 137.508) % 360}, 66%, 44%)`,
      ]),
    );
    const maximumAttendance = Math.max(
      1,
      ...data.nodes.map((node) => node.tournaments),
    );
    const maximumPartnerships = Math.max(
      1,
      ...data.edges.map((edge) => edge.tournaments),
    );
    const radii = new Map(
      data.nodes.map((node) => [
        node.id,
        attendanceRadius(node.tournaments, maximumAttendance),
      ]),
    );
    const getTheme = () => {
      const styles = window.getComputedStyle(canvas);
      return {
        label: styles.getPropertyValue("--network-label").trim(),
        outline: styles.getPropertyValue("--network-outline").trim(),
        edge: styles.getPropertyValue("--network-edge").trim(),
        strongEdge: styles.getPropertyValue("--network-strong-edge").trim(),
      };
    };
    let theme = getTheme();
    const edgeColor = (edge) => {
      const opacity = 0.2 + Math.min(4, edge.tournaments) * 0.16;
      return edge.tournaments >= 5
        ? theme.strongEdge
        : `rgba(${theme.edge}, ${opacity})`;
    };
    const nodes = new DataSet(
      data.nodes.map((node) => ({
        id: node.id,
        label: node.label,
        color: {
          background: colors.get(node.school_id),
          border: theme.outline,
        },
      })),
    );
    const edges = new DataSet(
      data.edges.map((edge, index) => ({
        id: index,
        from: edge.from,
        to: edge.to,
        color: { color: edgeColor(edge), inherit: false },
        width: partnershipWidth(edge.tournaments, maximumPartnerships),
      })),
    );
    const network = new Network(
      canvas,
      { nodes, edges },
      {
        nodes: {
          shape: "dot",
          size: 4,
          borderWidth: 1.2,
          font: {
            size: 12,
            color: theme.label,
            strokeWidth: 1.4,
            strokeColor: theme.outline,
          },
        },
        edges: { smooth: false, selectionWidth: 2 },
        layout: { improvedLayout: false },
        physics: false,
        interaction: {
          hover: true,
          hoverConnectedEdges: false,
          selectConnectedEdges: false,
          hideEdgesOnDrag: true,
          keyboard: { enabled: true, bindToWindow: false },
        },
      },
    );
    const labels = root.querySelector("[data-network-labels]");
    let selectionState = null;
    let motion = null;
    let axes = null;
    let freePositions = null;
    let worker = null;
    let animationFrame = null;
    let animationBounds = null;
    const refreshSizes = () => {
      const scale = Math.max(0.02, network.getScale());
      network.setOptions({
        nodes: {
          size: 4 / scale,
          borderWidth: 0.7,
          font: { size: 12 / scale, strokeWidth: 1.4 / scale },
        },
        edges: { selectionWidth: 1 / scale, hoverWidth: 0.6 / scale },
      });
      nodes.update(
        data.nodes.map((node) => ({
          id: node.id,
          size: radii.get(node.id) / scale,
          label:
            labels.checked &&
            (!selectionState?.visibleNames ||
              selectionState.visibleNames.has(node.id))
              ? node.label
              : "",
        })),
      );
      edges.update(
        data.edges.map((edge, index) => {
          const highlighted = selectionState?.isPathEdge(edge);
          let color = edgeColor(edge);
          if (selectionState?.pathEdges) color = `rgba(${theme.edge}, 0.15)`;
          if (highlighted) color = "#e86b16";
          return {
            id: index,
            width:
              Math.max(
                partnershipWidth(edge.tournaments, maximumPartnerships),
                highlighted ? 3.5 : 0,
              ) / scale,
            color: { color, highlight: color, hover: color, inherit: false },
          };
        }),
      );
    };
    root
      .querySelector("[data-network-zoom-in]")
      .addEventListener("click", () => {
        network.moveTo({ scale: network.getScale() * 1.5 });
        refreshSizes();
      });
    root
      .querySelector("[data-network-zoom-out]")
      .addEventListener("click", () => {
        network.moveTo({ scale: network.getScale() / 1.5 });
        refreshSizes();
      });
    const fit = () => {
      const base =
        animationBounds ||
        (motion?.getPositions().length
          ? motion.getPositions()
          : Object.values(network.getPositions()));
      const positions = [...base, ...(axes?.extent() || [])];
      const gutters = axes?.gutters() || { left: 0, right: 0, bottom: 0 };
      const left = Math.min(...positions.map((point) => point.x));
      const right = Math.max(...positions.map((point) => point.x));
      const top = Math.min(...positions.map((point) => point.y));
      const bottom = Math.max(...positions.map((point) => point.y));
      const scale =
        Math.min(
          (canvas.clientWidth - gutters.left - gutters.right) /
            (right - left + 80),
          (canvas.clientHeight - gutters.bottom) / (bottom - top + 80),
        ) * 0.9;
      network.moveTo({
        position: {
          x: (left + right) / 2 - (gutters.left - gutters.right) / (2 * scale),
          y: (top + bottom) / 2 + gutters.bottom / (2 * scale),
        },
        scale,
        animation: false,
      });
      refreshSizes();
    };
    axes = setupAxes(
      network,
      data,
      () => theme,
      () => {
        if (!freePositions || worker !== null) return;
        if (animationFrame !== null)
          window.cancelAnimationFrame(animationFrame);
        animationFrame = null;
        motion.suspend();
        axes.setBase(freePositions);
        const positions = axes.project(freePositions);
        animationBounds = positions;
        nodes.update(positions);
        fit();
        animationBounds = null;
        motion.start(positions, axes.constraints());
        pause.textContent = "Rearrange";
        pause.disabled = false;
        status.textContent = `${peopleCount} people · ${pairCount} partnerships`;
      },
    );
    document.addEventListener("standings:themechange", () => {
      theme = getTheme();
      network.setOptions({
        nodes: { font: { color: theme.label, strokeColor: theme.outline } },
      });
      nodes.update(
        data.nodes.map((node) => ({
          id: node.id,
          color: {
            background: colors.get(node.school_id),
            border: theme.outline,
          },
        })),
      );
      refreshSizes();
    });
    network.on("zoom", refreshSizes);
    selectionState = setupSelection(root, network, data, refreshSizes, radii);
    motion = setupMotion(root, network, nodes, refreshSizes);
    root.querySelector("[data-network-fit]").addEventListener("click", fit);
    labels.addEventListener("change", refreshSizes);
    let interrupted = false;
    let seed = 0;
    const arrange = () => {
      motion.suspend();
      pause.disabled = true;
      pause.textContent = "Arranging…";
      status.textContent = "Arranging connections…";
      worker = new Worker(
        new URL("./partnership_layout_worker.js", import.meta.url),
      );
      worker.onmessage = ({ data: { frames: rawFrames } }) => {
        worker.terminate();
        worker = null;
        freePositions = rawFrames[rawFrames.length - 1];
        axes.setBase(freePositions);
        const frames = rawFrames.map((frame) => axes.project(frame));
        const finalPositions = frames[frames.length - 1];
        animationBounds = finalPositions;
        nodes.update(frames[0]);
        fit();
        pause.textContent = "Settling…";
        const constraints = axes.constraints();
        const fixed = constraints.x && constraints.y;
        const duration =
          fixed || window.matchMedia("(prefers-reduced-motion: reduce)").matches
            ? 0
            : 2800;
        let start = null;
        let lastDraw = 0;
        const animate = (time) => {
          if (start === null) start = time;
          const progress = duration
            ? Math.min(1, (time - start) / duration)
            : 1;
          if (time - lastDraw >= 32 || progress === 1) {
            const frameIndex = progress * (frames.length - 1);
            const first = frames[Math.floor(frameIndex)];
            const second =
              frames[Math.min(frames.length - 1, Math.floor(frameIndex) + 1)];
            const fraction = frameIndex % 1;
            nodes.update(
              first.map((node, index) => ({
                id: node.id,
                x: node.x + (second[index].x - node.x) * fraction,
                y: node.y + (second[index].y - node.y) * fraction,
              })),
            );
            lastDraw = time;
          }
          if (progress < 1) {
            animationFrame = window.requestAnimationFrame(animate);
          } else {
            animationFrame = null;
            animationBounds = null;
            refreshSizes();
            motion.start(finalPositions, axes.constraints());
            status.textContent = `${peopleCount} people · ${pairCount} partnerships`;
            pause.textContent = "Rearrange";
            pause.disabled = false;
          }
        };
        animationFrame = window.requestAnimationFrame(animate);
      };
      worker.onerror = () => {
        worker.terminate();
        worker = null;
        status.textContent =
          "Could not arrange the network. Reload to try again.";
      };
      worker.postMessage({ ...data, seed });
      seed += 0.2;
    };
    pause.addEventListener("click", arrange);
    window.addEventListener("pagehide", () => {
      interrupted = worker !== null || animationFrame !== null;
      if (worker) worker.terminate();
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      worker = null;
      animationFrame = null;
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && interrupted) {
        interrupted = false;
        arrange();
      }
    });
    arrange();
  } catch {
    status.textContent = "Could not load the network. Reload to try again.";
    canvas.hidden = true;
  }
}

export default initPartnershipNetwork;
