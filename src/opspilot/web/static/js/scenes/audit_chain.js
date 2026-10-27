// Scene 5, the audit chain: the case's events as linked blocks in time order, each carrying
// its short hash; evidence digests are seals on run-completed blocks. After a verification,
// verified links are solid, unchecked ones dashed grey, and a broken link is red.
const PER_ROW = 6;

export function build(kit, data) {
  const { T, colors } = kit;
  const size = [1.7, 0.8, 0.4], step = 2.3, rowStep = 1.5;
  // Past 24 events, hashes show on the focused block only and blocks are outlined at the
  // front: 100 events (the page's limit) stay within 500 line segments.
  const many = data.blocks.length > 24;
  const place = (i) => {
    const row = Math.floor(i / PER_ROW), col = i % PER_ROW;
    return new T.Vector3(-((PER_ROW - 1) * step) / 2 + col * step, -row * rowStep, 0);
  };
  const items = data.blocks.map((block, i) => {
    const at = place(i);
    const edge = block.state === "broken" ? colors.negative : block.state === "verified" ? colors.text : colors.compare;
    const group = kit.add(new T.Group());
    group.position.copy(at);
    group.add(kit.box(...size, { fill: colors.surface, edge, outline: many ? "front" : "all" }));
    if (block.seal) {
      const seal = kit.box(0.22, 0.22, 0.06, { fill: colors.data });
      seal.position.set(size[0] / 2 - 0.22, size[1] / 2 - 0.2, size[2] / 2 + 0.03);
      group.add(seal);
    }
    if (i > 0) {
      const prev = place(i - 1), sameRow = prev.y === at.y;
      const a = sameRow ? prev.clone().setX(prev.x + size[0] / 2) : prev.clone().setY(prev.y - size[1] / 2);
      const b = sameRow ? at.clone().setX(at.x - size[0] / 2) : at.clone().setY(at.y + size[1] / 2);
      if (block.state === "broken") {
        const mid = a.clone().lerp(b, 0.5), back = a.clone().lerp(b, 0.3), on = a.clone().lerp(b, 0.7);
        kit.add(kit.rod(a, back, 0.035, colors.negative));
        kit.add(kit.rod(on, b, 0.035, colors.negative));
        kit.label("×", [mid.x, mid.y, mid.z], "breaks");
      } else {
        kit.add(kit.line([a, b], { color: block.state === "verified" ? colors.text : colors.compare, dashed: block.state !== "verified" }));
      }
    }
    const label = kit.label(block.hash, [at.x, at.y, at.z + size[2] / 2], `mono${many ? " on-focus" : ""}`);
    return kit.item({
      object: group,
      label,
      text: `Event ${block.seq}: ${block.event}, hash ${block.hash}, ${block.state}${block.seal ? `, evidence digest ${block.seal}` : ""}.`,
      activate: () => kit.go(block.target),
    });
  });
  const rows = Math.ceil(data.blocks.length / PER_ROW);
  const height = (rows - 1) * rowStep;
  return { items, orbit: false, camera: { target: [0, -height / 2, 0], fit: [PER_ROW * step, height + 1.6], yaw: 0, pitch: 0.3 } };
}
