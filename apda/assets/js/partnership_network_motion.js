export default function setupMotion(root, network, nodes, onDragEnd) {
  const button = root.querySelector("[data-network-motion]");
  let anchors = [];
  let handle = null;
  let dragging = false;
  let active = true;
  let lastDraw = 0;

  function cancel() {
    if (handle !== null) window.cancelAnimationFrame(handle);
    handle = null;
  }

  function tick(time) {
    const interval = anchors.length > 2000 ? 100 : 33;
    if (!dragging && time - lastDraw >= interval) {
      const scale = Math.max(0.02, network.getScale());
      nodes.update(
        anchors.map((node) => ({
          id: node.id,
          x: node.x + (Math.sin(time / 1400 + node.id * 1.7) * 2.2) / scale,
          y: node.y + (Math.cos(time / 1700 + node.id * 2.3) * 2.2) / scale,
        })),
      );
      lastDraw = time;
    }
    handle = window.requestAnimationFrame(tick);
  }

  function schedule() {
    cancel();
    if (active && anchors.length) handle = window.requestAnimationFrame(tick);
    button.textContent = active ? "Pause motion" : "Resume motion";
  }

  button.addEventListener("click", () => {
    active = !active;
    schedule();
  });
  network.on("dragStart", () => {
    dragging = true;
  });
  network.on("dragEnd", (event) => {
    if (event.nodes.length) {
      const positions = network.getPositions(event.nodes);
      anchors = anchors.map((node) =>
        positions[node.id] ? { id: node.id, ...positions[node.id] } : node,
      );
    }
    dragging = false;
    onDragEnd();
  });
  window.addEventListener("pagehide", cancel);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      dragging = false;
      schedule();
    }
  });
  return {
    getPositions: () => anchors,
    suspend: () => {
      cancel();
      button.disabled = true;
    },
    start: (positions) => {
      anchors = positions;
      button.disabled = false;
      schedule();
    },
  };
}
