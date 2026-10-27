// Scene 4, the pipeline track: the agent's steps as stations on a track, spaced by when
// each wrote its record. Quota waits are hatched grey gaps, so waiting can't hide; AI calls
// are small markers; while investigating, the current station is lit in the data colour.
export function build(kit, data) {
  const { T, colors } = kit;
  const length = 14, x = (p) => -length / 2 + p * length;
  kit.add(kit.box(length, 0.06, 0.3, { fill: colors.raised, edge: colors.compare }));
  for (const wait of data.waits) {
    const from = x(wait.from), to = x(wait.to);
    const gap = kit.add(new T.Group());
    const span = kit.box(Math.max(to - from, 0.08), 0.08, 0.5, { fill: colors.surface, edge: colors.compare });
    span.position.set((from + to) / 2, 0.02, 0);
    gap.add(span);
    for (let h = from; h < to; h += 0.18) gap.add(kit.line([[h, 0.07, 0.25], [Math.min(h + 0.25, to), 0.07, -0.25]], { color: colors.compare }));
  }
  for (const marker of data.markers) {
    const tick = kit.add(kit.box(0.05, 0.35, 0.05, { fill: colors.muted }));
    tick.position.set(x(marker.p), 0.3, -0.25);
  }
  const items = data.stations.map((station, i) => {
    const color = station.current ? colors.data : station.done ? colors.text : colors.compare;
    const group = kit.add(new T.Group());
    group.position.x = x(station.p);
    const post = kit.box(0.26, 0.5, 0.26, { fill: station.current || station.done ? color : colors.surface, edge: color });
    post.position.y = 0.25;
    group.add(post);
    const hit = kit.hit(0.6, 1.2, 0.8);
    hit.position.y = 0.4;
    group.add(hit);
    const label = kit.label(station.label, [x(station.p), i % 2 ? -0.55 : 0.95, 0], station.current ? "data" : "");
    const when = station.at === null ? (data.live ? "" : ", untimed") : `, recorded at +${station.at} s`;
    const state = station.current ? ", in progress" : station.done ? ", done" : "";
    return kit.item({
      object: group,
      label,
      text: `${station.label}${when}${state}.`,
      activate: () => { if (station.target) kit.go(station.target); },
    });
  });
  return { items, orbit: false, camera: { target: [0, 0.2, 0], fit: [length + 2.4, 2.2], yaw: 0, pitch: 0.42 } };
}
