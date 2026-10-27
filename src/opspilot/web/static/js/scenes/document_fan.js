// Scene 6, the document fan: the case's documents as fanned page stacks; the page in the
// viewer sits forward. Enter opens an invoice page in the viewer (a GET link); a supporting
// document is focused in the list, never downloaded (downloads are audited).
export function build(kit, data) {
  const { T, colors } = kit;
  const sheet = [1.3, 1.7, 0.03], spread = 0.22, between = 2.6;
  const left = -((data.documents.length - 1) * between) / 2;
  const items = [];
  data.documents.forEach((doc, d) => {
    const centre = left + d * between;
    const pages = doc.pages.slice(0, 12);
    kit.label(doc.label, [centre, -1.25, 0], "");
    pages.forEach((page, i) => {
      const group = kit.add(new T.Group());
      const offset = (i - (pages.length - 1) / 2) * spread;
      group.position.set(centre + offset, 0, page.forward ? 0.6 : -i * 0.04);
      group.rotation.z = -offset * 0.35;
      group.add(kit.box(...sheet, { fill: colors.surface, edge: page.forward ? colors.text : colors.compare }));
      const label = kit.label(page.n ? String(page.n) : "", [centre + offset, 0.05, group.position.z + 0.05], page.forward ? "strong" : "on-focus");
      items.push(kit.item({
        object: group,
        label,
        text: `${page.label}${page.forward ? ", in the viewer" : ""}.`,
        activate: () => (page.href ? kit.open(page.href) : kit.go(page.target)),
      }));
    });
  });
  const span = Math.max(data.documents.length * between, 5);
  return { items, orbit: false, camera: { target: [0, -0.2, 0], fit: [span, 3.4], yaw: 0, pitch: 0.12 } };
}
