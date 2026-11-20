import "../css/partnership_network.scss";

function tournamentLabel(count) {
  return `${count} recorded tournament${count === 1 ? "" : "s"}`;
}

async function initPartnershipNetwork() {
  const root = document.getElementById("partnership-network");
  if (!root) return;
  const status = root.querySelector("[data-network-status]");
  const canvas = root.querySelector(".partnership-network-canvas");
  const details = root.querySelector("[data-network-details]");
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
      details.textContent =
        "No partnerships match these filters. Try a wider season range or fewer filters.";
      canvas.hidden = true;
      return;
    }
    const byId = new Map(data.nodes.map((node) => [node.id, node]));
    const schools = [...new Set(data.nodes.map((node) => node.school_id))].sort(
      (a, b) => a - b,
    );
    const colors = new Map(
      schools.map((school, index) => [
        school,
        `hsl(${(index * 137.508) % 360}, 66%, 44%)`,
      ]),
    );
    const degree = new Map(data.nodes.map((node) => [node.id, 0]));
    data.edges.forEach((edge) => {
      degree.set(edge.from, degree.get(edge.from) + 1);
      degree.set(edge.to, degree.get(edge.to) + 1);
    });
    const labelOrder = [...data.nodes].sort(
      (a, b) => degree.get(b.id) - degree.get(a.id),
    );
    const nodes = new DataSet(
      data.nodes.map((node) => ({
        id: node.id,
        label: node.label,
        color: { background: colors.get(node.school_id), border: "#ffffff" },
      })),
    );
    const edges = new DataSet(
      data.edges.map((edge, index) => ({
        id: index,
        from: edge.from,
        to: edge.to,
        color: {
          color:
            edge.tournaments >= 5
              ? "#111827"
              : `rgba(55, 65, 81, ${0.2 + Math.min(4, edge.tournaments) * 0.16})`,
          inherit: false,
        },
        width: 0.6 + Math.min(5, edge.tournaments) * 0.5,
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
            color: "#182535",
            strokeWidth: 1.4,
            strokeColor: "#ffffff",
          },
        },
        edges: { smooth: false, selectionWidth: 2 },
        layout: { improvedLayout: false },
        physics: false,
        interaction: {
          hover: true,
          hideEdgesOnDrag: true,
          keyboard: { enabled: true, bindToWindow: false },
        },
      },
    );
    const labels = root.querySelector("[data-network-labels]");
    let hovered = null;
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
      });
      const positions = network.getPositions();
      const occupied = [];
      const visibleLabels = new Set();
      const ordered =
        hovered === null
          ? labelOrder
          : [
              byId.get(hovered),
              ...labelOrder.filter((node) => node.id !== hovered),
            ];
      if (labels.checked) {
        ordered.forEach((node) => {
          const point = network.canvasToDOM(positions[node.id]);
          const fontSize = 12;
          const halfWidth = node.label.length * fontSize * 0.29 + 4;
          const top = point.y + 6;
          const box = {
            left: point.x - halfWidth,
            right: point.x + halfWidth,
            top,
            bottom: top + fontSize + 4,
          };
          if (
            box.left < 4 ||
            box.right > canvas.clientWidth - 4 ||
            box.top < 0 ||
            box.bottom > canvas.clientHeight
          )
            return;
          const overlaps = occupied.some(
            (other) =>
              box.left < other.right &&
              box.right > other.left &&
              box.top < other.bottom &&
              box.bottom > other.top,
          );
          if (!overlaps || node.id === hovered) {
            occupied.push(box);
            visibleLabels.add(node.id);
          }
        });
      }
      nodes.update(
        data.nodes.map((node) => ({
          id: node.id,
          label: visibleLabels.has(node.id) ? node.label : "",
        })),
      );
      edges.update(
        data.edges.map((edge, index) => ({
          id: index,
          width: (0.45 + Math.min(5, edge.tournaments) * 0.24) / scale,
        })),
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
      const positions =
        animationBounds || Object.values(network.getPositions());
      const left = Math.min(...positions.map((point) => point.x));
      const right = Math.max(...positions.map((point) => point.x));
      const top = Math.min(...positions.map((point) => point.y));
      const bottom = Math.max(...positions.map((point) => point.y));
      const scale =
        Math.min(
          1.3,
          canvas.clientWidth / (right - left + 80),
          canvas.clientHeight / (bottom - top + 80),
        ) * 0.9;
      network.moveTo({
        position: { x: (left + right) / 2, y: (top + bottom) / 2 },
        scale,
        animation: false,
      });
      refreshSizes();
    };
    network.on("zoom", refreshSizes);
    network.on("dragEnd", refreshSizes);
    network.on("hoverNode", ({ node }) => {
      hovered = node;
      refreshSizes();
    });
    network.on("blurNode", () => {
      hovered = null;
      refreshSizes();
    });
    root.querySelector("[data-network-fit]").addEventListener("click", fit);
    labels.addEventListener("change", refreshSizes);
    let worker = null;
    let seed = 0;
    const arrange = () => {
      pause.disabled = true;
      pause.textContent = "Arranging…";
      status.textContent = "Arranging connections…";
      worker = new Worker(
        new URL("./partnership_layout_worker.js", import.meta.url),
      );
      worker.onmessage = ({ data: { frames } }) => {
        worker.terminate();
        worker = null;
        const finalPositions = frames[frames.length - 1];
        animationBounds = finalPositions;
        nodes.update(frames[0]);
        fit();
        pause.textContent = "Settling…";
        const duration = window.matchMedia("(prefers-reduced-motion: reduce)")
          .matches
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
      if (worker) worker.terminate();
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
    });
    arrange();
    network.on("click", (selection) => {
      details.replaceChildren();
      if (selection.nodes.length) {
        const node = byId.get(selection.nodes[0]);
        const link = document.createElement("a");
        link.href = node.url;
        link.textContent = node.label;
        details.append(
          link,
          document.createTextNode(
            [
              ` · ${node.schools.join(" / ") || "Unaffiliated"}`,
              ` · ${tournamentLabel(node.tournaments)}`,
              node.qualified ? " · Nationals qualified" : "",
            ].join(""),
          ),
        );
      } else if (selection.edges.length) {
        const edge = data.edges[selection.edges[0]];
        const names = `${byId.get(edge.from).label} and ${byId.get(edge.to).label}`;
        details.textContent = `${names} · ${tournamentLabel(edge.tournaments)} together`;
      } else {
        details.textContent = "Select a person or partnership to explore.";
      }
    });
  } catch {
    status.textContent = "Could not load the network. Reload to try again.";
    canvas.hidden = true;
  }
}

export default initPartnershipNetwork;
