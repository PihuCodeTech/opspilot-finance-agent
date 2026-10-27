// Scene 1, the match stack (and 7, the sign-in hero): PO, receipt and invoice as three
// sheets in depth, each invoice line threaded through all three. A line that breaks a check
// shows a break between the disagreeing planes, labelled with the difference.
export function build(kit, data) {
  const { T, colors } = kit;
  const count = Math.max(data.lines.length, 1);
  const width = Math.max(6, count * 1.5), depth = 3, gap = 1.5;
  const level = { po: 0, receipt: gap, invoice: 2 * gap };
  for (const plane of data.planes) {
    const front = plane.key === "invoice";
    const sheet = kit.add(kit.box(width, 0.04, depth, { fill: colors.surface, edge: front ? colors.text : colors.compare, opacity: 0.9 }));
    sheet.position.y = level[plane.key];
    kit.label(plane.label, [width / 2 + 0.2, level[plane.key], depth / 2], "", "left");
  }
  const items = data.lines.map((line, k) => {
    const x = -width / 2 + (k + 0.5) * (width / count);
    const at = (key) => new T.Vector3(x, level[key], 0);
    const group = kit.add(new T.Group());
    group.add(kit.line([at("po"), at("receipt"), at("invoice")], { color: colors.compare }));
    const value = kit.box(0.16, 0.16, 0.16, { fill: colors.data }); // the invoice's own value
    value.position.copy(at("invoice"));
    group.add(value);
    for (const b of line.breaks) {
      const color = b.status === "inconclusive" ? colors.warning : colors.negative;
      if (b.from === b.to) {
        const mark = kit.box(0.3, 0.3, 0.3, { edge: color });
        mark.position.copy(at(b.to));
        group.add(mark);
      } else {
        // Two rods with a gap between them: the thread is broken between these planes.
        const low = at(b.from), high = at(b.to), mid = low.clone().lerp(high, 0.5);
        group.add(kit.rod(low, mid.clone().setY(mid.y - 0.18), 0.045, color));
        group.add(kit.rod(mid.clone().setY(mid.y + 0.18), high, 0.045, color));
      }
      if (!data.idle) kit.label(b.text, [x, (level[b.from] + level[b.to]) / 2, 0], "breaks", "left");
    }
    const hit = kit.hit(width / count, 2 * gap + 0.6, 0.8);
    hit.position.set(x, gap, 0);
    group.add(hit);
    if (data.idle) return null; // the hero is decorative: nothing to focus
    const tag = kit.label(String(line.line), [x, 2 * gap + 0.4, 0]);
    const breaks = line.breaks.map((b) => b.text).join("; ");
    return kit.item({
      object: group,
      label: tag,
      text: `Line ${line.line}, ${line.description}: PO ${line.values.po}, received ${line.values.receipt}, invoice ${line.values.invoice}. ${breaks || "Matches."}`,
      activate: () => { kit.mark(line.row); kit.go(line.finding || line.row); },
    });
  }).filter(Boolean);
  return {
    items,
    orbit: !data.idle,
    idle: data.idle || 0,
    camera: { target: [0, gap, 0], fit: [width + 3, 2 * gap + 2.4], depth, yaw: -0.55, pitch: 0.42 },
  };
}
