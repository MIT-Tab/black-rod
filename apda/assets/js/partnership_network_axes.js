const metrics = {
  toty_points: "TOTY points",
  qual_points: "Qual points",
  first_year: "First year in league",
  final_year: "Final year in league",
  tournaments: "Tournaments attended",
  partner_count: "Partners",
  school: "School",
  qualification_count: "Nationals-qualified seasons",
};

export function describeAxisValue(key, node) {
  if (!key) return "None";
  let value = node[key];
  if (key === "school") value = node.schools.join(" / ") || "Unaffiliated";
  if (value === null || value === undefined) value = "Unknown";
  if (typeof value === "number") {
    value = value.toLocaleString(undefined, {
      maximumFractionDigits: 2,
      useGrouping: !key.endsWith("year"),
    });
  }
  return `${metrics[key]}: ${value}`;
}

function makeScale(key, nodes, start, end) {
  const value = (node) =>
    key === "school" ? node.schools.join(" / ") || "Unaffiliated" : node[key];
  const known = nodes
    .map(value)
    .filter((entry) => entry !== null && entry !== undefined);
  const unknown = known.length !== nodes.length;
  const first = unknown ? start + (end - start) * 0.16 : start;
  let ticks;
  let coordinate;
  if (key === "school") {
    const categories = [...new Set(known)].sort((a, b) => a.localeCompare(b));
    coordinate = (entry) =>
      categories.length === 1
        ? (first + end) / 2
        : first +
          ((end - first) * categories.indexOf(entry)) / (categories.length - 1);
    ticks = categories.map((entry) => ({
      label: entry,
      position: coordinate(entry),
    }));
  } else {
    const minimum = Math.min(...known);
    const maximum = Math.max(...known);
    coordinate = (entry) =>
      maximum === minimum
        ? (first + end) / 2
        : first + ((end - first) * (entry - minimum)) / (maximum - minimum);
    const values = new Set();
    if (known.length) {
      for (let index = 0; index <= 4; index += 1) {
        const number = minimum + ((maximum - minimum) * index) / 4;
        const precision = key.endsWith("points") ? 100 : 1;
        values.add(
          Math.max(
            minimum,
            Math.min(maximum, Math.round(number * precision) / precision),
          ),
        );
      }
    }
    ticks = [...values].map((entry) => ({
      label: entry.toLocaleString(undefined, {
        maximumFractionDigits: 2,
        useGrouping: !key.endsWith("year"),
      }),
      position: coordinate(entry),
    }));
  }
  if (unknown) ticks.unshift({ label: "Unknown", position: start });
  return {
    title: metrics[key],
    ticks,
    position: (node) =>
      value(node) === null || value(node) === undefined
        ? start
        : coordinate(value(node)),
  };
}

function visibleTicks(scale, ctx, zoom, horizontal) {
  const candidates = scale.ticks
    .map((tick) => {
      const label =
        tick.label.length > 22 ? `${tick.label.slice(0, 21)}…` : tick.label;
      return {
        ...tick,
        label,
        halfSize: horizontal ? ctx.measureText(label).width / 2 : 7 / zoom,
      };
    })
    .sort((a, b) => a.position - b.position);
  const last = candidates[candidates.length - 1];
  let previousRight = -Infinity;
  return candidates.filter((tick, index) => {
    const left = tick.position - tick.halfSize;
    const right = tick.position + tick.halfSize;
    if (left < previousRight + 6 / zoom) return false;
    if (
      index > 0 &&
      tick !== last &&
      right + 6 / zoom > last.position - last.halfSize
    )
      return false;
    previousRight = right;
    return true;
  });
}

export default function setupAxes(network, data, getTheme, onChange) {
  const xField = document.querySelector('[data-network-axis="x"]');
  const yField = document.querySelector('[data-network-axis="y"]');
  const byId = new Map(data.nodes.map((node) => [node.id, node]));
  let base = null;
  let bounds = null;
  let xScale = null;
  let yScale = null;

  function rebuild() {
    if (!base) return;
    const xs = base.map((point) => point.x);
    const ys = base.map((point) => point.y);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    const width = Math.max(600, Math.max(...xs) - Math.min(...xs));
    const height = Math.max(500, Math.max(...ys) - Math.min(...ys));
    bounds = {
      left: centerX - width / 2,
      right: centerX + width / 2,
      top: centerY - height / 2,
      bottom: centerY + height / 2,
    };
    xScale = xField.value
      ? makeScale(xField.value, data.nodes, bounds.left, bounds.right)
      : null;
    yScale = yField.value
      ? makeScale(yField.value, data.nodes, bounds.bottom, bounds.top)
      : null;
  }

  [xField, yField].forEach((field) =>
    field.addEventListener("change", () => {
      rebuild();
      const url = new URL(window.location.href);
      [xField, yField].forEach((input) => {
        if (input.value) url.searchParams.set(input.name, input.value);
        else url.searchParams.delete(input.name);
      });
      window.history.replaceState(null, "", url);
      onChange();
    }),
  );

  network.on("beforeDrawing", (ctx) => {
    if (!bounds || (!xScale && !yScale)) return;
    const zoom = Math.max(0.0001, network.getScale());
    const theme = getTheme();
    ctx.save();
    ctx.font = `${12 / zoom}px sans-serif`;
    ctx.fillStyle = theme.label;
    ctx.strokeStyle = `rgba(${theme.edge}, 0.5)`;
    ctx.lineWidth = 1 / zoom;
    const xLine = bounds.bottom + 28 / zoom;
    const yLine = bounds.left - 28 / zoom;
    if (xScale) {
      ctx.beginPath();
      ctx.moveTo(bounds.left, xLine);
      ctx.lineTo(bounds.right, xLine);
      ctx.stroke();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      visibleTicks(xScale, ctx, zoom, true).forEach((tick) => {
        ctx.beginPath();
        ctx.moveTo(tick.position, xLine);
        ctx.lineTo(tick.position, xLine + 5 / zoom);
        ctx.stroke();
        ctx.fillText(tick.label, tick.position, xLine + 9 / zoom);
      });
      ctx.fillText(
        xScale.title,
        (bounds.left + bounds.right) / 2,
        xLine + 29 / zoom,
      );
    }
    if (yScale) {
      ctx.beginPath();
      ctx.moveTo(yLine, bounds.top);
      ctx.lineTo(yLine, bounds.bottom);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      visibleTicks(yScale, ctx, zoom, false).forEach((tick) => {
        ctx.beginPath();
        ctx.moveTo(yLine - 5 / zoom, tick.position);
        ctx.lineTo(yLine, tick.position);
        ctx.stroke();
        ctx.fillText(tick.label, yLine - 9 / zoom, tick.position);
      });
      ctx.translate(
        yLine - (yField.value === "school" ? 130 : 78) / zoom,
        (bounds.top + bounds.bottom) / 2,
      );
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.fillText(yScale.title, 0, 0);
    }
    ctx.restore();
  });

  return {
    setBase: (positions) => {
      base = positions;
      rebuild();
    },
    constraints: () => ({ x: Boolean(xScale), y: Boolean(yScale) }),
    project: (positions) =>
      positions.map((point) => ({
        id: point.id,
        x: xScale ? xScale.position(byId.get(point.id)) : point.x,
        y: yScale ? yScale.position(byId.get(point.id)) : point.y,
      })),
    extent: () =>
      xScale || yScale
        ? [
            { x: bounds.left, y: bounds.top },
            { x: bounds.right, y: bounds.bottom },
          ]
        : [],
    gutters: () => {
      let left = xScale ? 40 : 0;
      if (yScale) left = yField.value === "school" ? 185 : 130;
      return { left, right: xScale ? 40 : 0, bottom: xScale ? 90 : 0 };
    },
  };
}
