// Scene 3, the resolution chain: each finding as a card at the front, linked back to its
// resolution and that resolution's evidence. Links are dashed until the HTML checkbox for
// the resolution is ticked; the checkbox, never the scene, accepts anything.
export function build(kit, data) {
  const { T, colors } = kit;
  const row = 1.0, card = [3, 0.6, 0.08];
  // Columns step right and back: the finding at the front, its evidence furthest away.
  const column = { finding: [0, 0], resolution: [3.8, -1.6], evidence: [7.6, -3.2] };
  let y = 0;
  const items = data.items.map((item) => {
    const accepted = document.getElementById(item.accept)?.checked === true;
    const evidence = item.evidence.length ? item.evidence : ["—"];
    const top = y;
    y -= Math.max(1, evidence.length) * row;
    const middle = (top + y + row) / 2;
    const group = kit.add(new T.Group());
    const place = (text, [x, z], yy, strong) => {
      const box = kit.box(...card, { fill: colors.surface, edge: strong ? colors.text : colors.compare });
      box.position.set(x, yy, z);
      group.add(box);
      return kit.label(text, [x, yy, z + 0.06], strong ? "strong" : "");
    };
    const link = (a, b) => group.add(kit.line([a, b], { color: accepted ? colors.text : colors.compare, dashed: !accepted }));
    const edge = ([x, z], yy, side) => [x + (side * card[0]) / 2, yy, z];
    const label = place(item.finding, column.finding, middle, true);
    place(item.resolution, column.resolution, middle, false);
    link(edge(column.finding, middle, 1), edge(column.resolution, middle, -1));
    evidence.forEach((text, i) => {
      const ey = top - i * row;
      place(text, column.evidence, ey, false);
      link(edge(column.resolution, middle, 1), edge(column.evidence, ey, -1));
    });
    return kit.item({
      object: group,
      label,
      text: `${item.finding}: ${item.resolution}, evidence ${item.evidence.join(", ") || "none"}. ${accepted ? "Accepted." : "Not accepted yet."}`,
      activate: () => kit.go(item.accept),
    });
  });
  kit.listen(document, "change", (event) => { if (event.target.matches?.("input[data-accept]")) kit.refresh(); });
  const height = Math.max(-y, row);
  return { items, orbit: false, camera: { target: [3.8, -height / 2 + row / 2, -1.6], fit: [7.6 + card[0] + 0.6, height + 0.8], depth: 3.2, yaw: 0.22, pitch: 0.14 } };
}
