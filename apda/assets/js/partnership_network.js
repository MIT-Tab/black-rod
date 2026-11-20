import "../css/partnership_network.scss";

const palette = [
  "#31688e",
  "#8b5bb1",
  "#29a18c",
  "#e08b35",
  "#c45978",
  "#629447",
  "#697fc4",
  "#a67548",
];

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
    const maximum = data.edges.reduce(
      (highest, edge) => Math.max(highest, edge.tournaments),
      1,
    );
    const nodes = new DataSet(
      data.nodes.map((node) => ({
        id: node.id,
        label: node.label,
        color: palette[node.school_id % palette.length],
      })),
    );
    const edges = data.edges.map((edge, index) => {
      const darkness =
        0.16 + (0.74 * Math.log1p(edge.tournaments)) / Math.log1p(maximum);
      return {
        id: index,
        from: edge.from,
        to: edge.to,
        color: { color: `rgba(42, 57, 72, ${darkness})`, inherit: false },
        width: 0.6 + (2 * Math.log1p(edge.tournaments)) / Math.log1p(maximum),
      };
    });
    const network = new Network(
      canvas,
      { nodes, edges },
      {
        nodes: {
          shape: "dot",
          size: 5,
          borderWidth: 1,
          font: { size: 12, color: "#233747" },
        },
        edges: { smooth: false, selectionWidth: 2 },
        layout: { improvedLayout: false, randomSeed: 42 },
        physics: {
          solver: "barnesHut",
          barnesHut: {
            gravitationalConstant: -3500,
            springLength: 85,
            avoidOverlap: 0.4,
          },
          stabilization: { iterations: 150 },
        },
        interaction: {
          hover: true,
          hideEdgesOnDrag: true,
          keyboard: { enabled: true, bindToWindow: false },
        },
      },
    );
    root
      .querySelector("[data-network-zoom-in]")
      .addEventListener("click", () => {
        network.moveTo({ scale: network.getScale() * 1.5 });
      });
    root
      .querySelector("[data-network-zoom-out]")
      .addEventListener("click", () => {
        network.moveTo({ scale: network.getScale() / 1.5 });
      });
    let moving = true;
    pause.disabled = false;
    pause.addEventListener("click", () => {
      moving = !moving;
      network.setOptions({ physics: { enabled: moving } });
      pause.textContent = moving ? "Pause motion" : "Resume motion";
    });
    root
      .querySelector("[data-network-fit]")
      .addEventListener("click", () => network.fit());
    root
      .querySelector("[data-network-labels]")
      .addEventListener("change", (event) => {
        nodes.update(
          data.nodes.map((node) => ({
            id: node.id,
            label: event.target.checked ? node.label : "",
          })),
        );
      });
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
    network.on("stabilizationIterationsDone", () => network.fit());
  } catch {
    status.textContent = "Could not load the network. Reload to try again.";
    canvas.hidden = true;
  }
}

export default initPartnershipNetwork;
