// Scene 2, the desk overview: work as piles of invoice sheets, one pile per status (or per
// approval group). Height follows the count; the piles that need the viewer are in ink.
export function build(kit, data) {
  const { T, colors } = kit;
  const tallest = 2.4;
  // Spread the piles across the strip: the scene is as tall as the tallest pile and its labels.
  const visible = (tallest + 1.4) * (kit.aspect || 4);
  const spacing = Math.max(2.4, Math.min(5, (visible * 0.9) / Math.max(data.piles.length, 1)));
  const left = -((data.piles.length - 1) * spacing) / 2;
  const items = data.piles.map((pile, i) => {
    const height = 0.12 + pile.share * tallest;
    const ink = pile.ink ? colors.text : colors.compare;
    const group = kit.add(new T.Group());
    group.position.set(left + i * spacing, 0, 0);
    const stack = kit.box(1.5, height, 1.1, { fill: colors.surface, edge: ink });
    stack.position.y = height / 2;
    group.add(stack);
    // Sheet edges on the front face, one per case up to a dozen, then evenly.
    const sheets = Math.min(pile.count, 12);
    for (let s = 1; s < sheets; s++) {
      const y = (height * s) / sheets;
      group.add(kit.line([[-0.75, y, 0.56], [0.75, y, 0.56]], { color: ink }));
    }
    const hit = kit.hit(1.7, height + 0.6, 1.3);
    hit.position.y = height / 2;
    group.add(hit);
    const count = kit.label(String(pile.count), [group.position.x, height + 0.35, 0], pile.ink ? "strong" : "");
    kit.label(pile.label, [group.position.x, -0.35, 0.6], pile.current ? "strong" : "");
    const raise = (on) => {
      group.position.y = on ? 0.15 : 0;
      count.position.y = height + 0.35 + group.position.y;
    };
    let focused = false, hovered = false;
    return kit.item({
      object: group,
      label: count,
      text: `${pile.label}: ${pile.count}.${pile.href ? " Enter shows them in the table." : ""}`,
      activate: () => { if (pile.href) kit.open(pile.href); },
      focus: (on) => { focused = on; raise(focused || hovered); },
      hover: (on) => { hovered = on; raise(focused || hovered); },
    });
  });
  const span = Math.max(data.piles.length * spacing, 6);
  return { items, orbit: false, fillsWidth: true, camera: { target: [0, 1.1, 0], fit: [span, tallest + 1.4], depth: 1.1, yaw: 0, pitch: 0.28 } };
}
