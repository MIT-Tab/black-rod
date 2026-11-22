import { describeAxisValue } from "./partnership_network_axes";

function tournamentLabel(count) {
  return `${count} recorded tournament${count === 1 ? "" : "s"}`;
}

function edgeKey(first, second) {
  return first < second ? `${first}-${second}` : `${second}-${first}`;
}

export default function setupSelection(root, network, data, onChange, radii) {
  const details = root.querySelector("[data-network-details]");
  const byId = new Map(data.nodes.map((node) => [node.id, node]));
  const neighbors = new Map(data.nodes.map((node) => [node.id, new Set()]));
  data.edges.forEach((edge) => {
    neighbors.get(edge.from).add(edge.to);
    neighbors.get(edge.to).add(edge.from);
  });
  const state = { visibleNames: null, pathEdges: null };
  let firstEndpoint = null;
  let request = null;
  let axisDetails = null;
  let selectedPerson = null;
  const axisFields = ["x", "y"].map((axis) =>
    document.querySelector(`[data-network-axis="${axis}"]`),
  );

  function refreshAxisDetails() {
    if (!axisDetails?.isConnected) return;
    axisDetails.replaceChildren();
    axisFields.forEach((field, index) => {
      const line = document.createElement("div");
      line.textContent = `${index === 0 ? "X" : "Y"} axis · ${describeAxisValue(field.value, selectedPerson)}`;
      axisDetails.append(line);
    });
  }

  function appendAxisDetails(node) {
    selectedPerson = node;
    axisDetails = document.createElement("div");
    axisDetails.className = "mt-2";
    axisDetails.dataset.networkAxisValues = "";
    details.append(axisDetails);
    refreshAxisDetails();
  }

  axisFields.forEach((field) =>
    field.addEventListener("change", refreshAxisDetails),
  );

  function cancelRequest() {
    if (request) request.abort();
    request = null;
  }

  function clear() {
    cancelRequest();
    state.visibleNames = null;
    state.pathEdges = null;
    firstEndpoint = null;
    network.unselectAll();
    details.textContent = "Select a person or partnership to explore.";
    onChange();
  }

  async function showPaths(first, second) {
    const controller = new AbortController();
    request = controller;
    details.textContent = `Finding fastest paths between ${byId.get(first).label} and ${byId.get(second).label}…`;
    const url = new URL(root.dataset.pathsUrl, window.location.origin);
    url.search = new URL(root.dataset.url, window.location.origin).search;
    url.searchParams.set("first_debater", first);
    url.searchParams.set("second_debater", second);
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error("Path search failed");
      const result = await response.json();
      if (request !== controller) return;
      request = null;
      state.visibleNames = new Set([first, second]);
      state.pathEdges = new Set();
      details.replaceChildren();
      const heading = document.createElement("h2");
      heading.className = "h6";
      heading.textContent = `Fastest paths between ${byId.get(first).label} and ${byId.get(second).label}`;
      details.append(heading);
      if (!result.paths.length) {
        details.append(
          "No partnership path connects these people with the current filters.",
        );
      } else {
        const list = document.createElement("ol");
        result.paths.forEach((path) => {
          const item = document.createElement("li");
          item.className = "mb-2";
          const jumps = path.length - 1;
          item.append(`${jumps} partnership${jumps === 1 ? "" : "s"} · `);
          path.forEach((person, index) => {
            state.visibleNames.add(person.id);
            if (index) {
              item.append(" → ");
              state.pathEdges.add(edgeKey(path[index - 1].id, person.id));
            }
            const link = document.createElement("a");
            link.href = person.url;
            link.textContent = person.label;
            item.append(link);
          });
          list.append(item);
        });
        details.append(list);
      }
      onChange();
    } catch (error) {
      if (error.name !== "AbortError" && request === controller) {
        request = null;
        details.textContent =
          "Could not load paths. Shift-click two people to try again.";
      }
    }
  }

  function clickedNode(selection) {
    if (!selection.pointer) return selection.nodes[0];
    const point = network.DOMtoCanvas(selection.pointer.DOM);
    const positions = network.getPositions();
    const scale = network.getScale();
    let closest;
    let distance = Infinity;
    data.nodes.forEach((node) => {
      const position = positions[node.id];
      const candidate =
        Math.hypot(position.x - point.x, position.y - point.y) * scale;
      if (candidate <= radii.get(node.id) + 2 && candidate < distance) {
        closest = node.id;
        distance = candidate;
      }
    });
    return closest ?? network.getNodeAt(selection.pointer.DOM);
  }

  root.querySelector("[data-network-clear]").addEventListener("click", clear);
  network.on("click", (selection) => {
    cancelRequest();
    const id = clickedNode(selection);
    if (id !== undefined) {
      if (selection.event?.srcEvent?.shiftKey) {
        if (firstEndpoint === null || firstEndpoint === id) {
          firstEndpoint = id;
          state.pathEdges = null;
          state.visibleNames = null;
          details.textContent = `Shift-click a second person to find paths from ${byId.get(id).label}.`;
          appendAxisDetails(byId.get(id));
          network.selectNodes([id], false);
        } else {
          const first = firstEndpoint;
          firstEndpoint = null;
          network.selectNodes([first, id], false);
          showPaths(first, id);
        }
      } else {
        network.selectNodes([id], false);
        firstEndpoint = null;
        state.pathEdges = null;
        state.visibleNames = neighbors.get(id);
        const node = byId.get(id);
        const link = document.createElement("a");
        link.href = node.url;
        link.textContent = node.label;
        details.replaceChildren(link);
        details.append(
          ` · ${node.schools.join(" / ") || "Unaffiliated"} · ${tournamentLabel(node.tournaments)}`,
          node.qualified ? " · Nationals qualified" : "",
        );
        appendAxisDetails(node);
      }
      onChange();
    } else if (selection.edges.length) {
      const edge = data.edges[selection.edges[0]];
      const names = `${byId.get(edge.from).label} and ${byId.get(edge.to).label}`;
      details.textContent = `${names} · ${tournamentLabel(edge.tournaments)} together`;
    } else {
      clear();
    }
  });
  window.addEventListener("pagehide", cancelRequest);
  state.isPathEdge = (edge) =>
    state.pathEdges && state.pathEdges.has(edgeKey(edge.from, edge.to));
  return state;
}
